#include "schematic_sim_models.hpp"

#include "spice_subcircuit.hpp"

#include <cctype>
#include <memory>
#include <optional>
#include <set>
#include <utility>

// KiCad and wx headers aren't built against this project's strict warning settings; see
// libkicad.cpp.
#if defined(__clang__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Weverything"
#endif

#include <ki_exception.h>
#include <locale_io.h>
#include <kiid.h>
#include <project.h>
#include <project/project_file.h>
#include <reporter.h>
#include <richio.h> // sch_io_kicad_sexpr.h relies on it without including it
#include <sch_io/kicad_sexpr/sch_io_kicad_sexpr.h>
#include <sch_screen.h>
#include <sch_sheet.h>
#include <sch_sheet_path.h>
#include <sch_symbol.h>
#include <schematic.h>
#include <sim/sim_lib_mgr.h>
#include <sim/sim_library.h>
#include <sim/sim_model.h>
#include <sim/sim_model_raw_spice.h>
#include <sim/sim_model_subckt.h>
#include <wx/filename.h>

#if defined(__clang__)
#pragma clang diagnostic pop
#endif

namespace libkicad::detail {
namespace {

std::string _utf8(const wxString& text) {
    return std::string(text.utf8_str());
}

/// Collects the warnings and errors KiCad reports while resolving one symbol's model.
class MessageReporter : public REPORTER {
public:
    REPORTER& Report(const wxString& text, SEVERITY severity = RPT_SEVERITY_UNDEFINED) override {
        REPORTER::Report(text, severity);
        if (severity == RPT_SEVERITY_ERROR || severity == RPT_SEVERITY_WARNING) {
            if (!messages.empty()) messages += '\n';
            messages += _utf8(text);
        }
        return *this;
    }

    std::string messages;
};

/// SCHEMATIC attaches its own ERC and schematic settings to the project file and only detaches
/// them in SetProject(); its destructor leaves them behind, where the next SCHEMATIC on the same
/// project would stack a second copy.
struct SchematicDeleter {
    void operator()(SCHEMATIC* schematic) const {
        schematic->SetProject(nullptr);
        delete schematic;
    }
};

using SchematicPtr = std::unique_ptr<SCHEMATIC, SchematicDeleter>;

/// The parts of SCH_EDIT_FRAME::OpenProjectFiles() that symbol references, field text and
/// simulation models depend on -- notably its handling of a multi-root project, whose top-level
/// sheets are listed in the project file (the .kicad_sch named after the project is then only a
/// stub). PROJECT_FILE migrates a single-root project to that form as it loads, listing its root
/// sheet, so `rootPath` is only a fallback. EESCHEMA_HELPERS::LoadSchematic(), which kicad-cli
/// uses, only loads the one file, and also builds a TOOL_MANAGER it never frees. Like the board
/// loader, this uses the KiCad format plugin directly rather than SCH_IO_MGR, whose registry links
/// every foreign-format importer.
SchematicPtr _loadSchematic(PROJECT& project, const std::string& rootPath, std::string& error) {
    LOCALE_IO cLocale;
    SchematicPtr schematic(new SCHEMATIC(&project));
    SCH_IO_KICAD_SEXPR plugin;
    std::vector<SCH_SHEET*> topLevelSheets;
    std::string loading = rootPath;
    std::string missing;
    try {
        const std::vector<TOP_LEVEL_SHEET_INFO>& listedSheets = project.GetProjectFile().GetTopLevelSheets();
        if (listedSheets.empty()) {
            if (SCH_SHEET* root = plugin.LoadSchematicFile(wxString::FromUTF8(rootPath), schematic.get())) {
                topLevelSheets.push_back(root);
            }
        }
        for (const TOP_LEVEL_SHEET_INFO& info : listedSheets) {
            const wxFileName file(project.GetProjectPath(), info.filename);
            loading = _utf8(file.GetFullPath());
            // The editor skips a listed sheet whose file is missing, too.
            if (!file.FileExists()) {
                if (missing.empty()) missing = loading;
                continue;
            }
            if (SCH_SHEET* sheet = plugin.LoadSchematicFile(file.GetFullPath(), schematic.get())) {
                // niluuid is a placeholder for "keep the file's own UUID". Symbol instance paths
                // start from the project's UUID for the sheet, so it must win over the file's.
                if (info.uuid != niluuid) const_cast<KIID&>(sheet->m_Uuid) = info.uuid;
                sheet->SetName(info.name);
                topLevelSheets.push_back(sheet);
            }
        }
    } catch (const IO_ERROR& exception) {
        error = "Could not load the KiCad schematic \"" + loading + "\": " + _utf8(exception.What());
        return nullptr;
    } catch (const std::exception& exception) {
        error = "Could not load the KiCad schematic \"" + loading + "\": " + exception.what();
        return nullptr;
    }
    if (topLevelSheets.empty()) {
        error = missing.empty() ? "Could not load the KiCad schematic \"" + rootPath + "\""
                                : "The KiCad schematic file does not exist: \"" + missing + "\"";
        return nullptr;
    }
    schematic->SetTopLevelSheets(topLevelSheets);

    SCH_SHEET_LIST sheets = schematic->BuildSheetListSortedByPageNumbers();
    SCH_SCREENS screens(schematic->Root());
    for (SCH_SCREEN* screen = screens.GetFirst(); screen; screen = screens.GetNext()) {
        screen->UpdateLocalLibSymbolLinks();
    }
    // Older files keep symbol and sheet instance data (references, per-instance values) on the
    // root screen rather than on each item.
    if (SCH_SCREEN* rootScreen = schematic->RootScreen()) {
        if (rootScreen->GetFileFormatVersionAtLoad() < 20221002) {
            sheets.UpdateSymbolInstanceData(rootScreen->GetSymbolInstances());
        }
        if (rootScreen->GetFileFormatVersionAtLoad() < 20221110) {
            sheets.UpdateSheetInstanceData(rootScreen->GetSheetInstances());
        }
    }
    for (SCH_SCREEN* screen = screens.GetFirst(); screen; screen = screens.GetNext()) {
        screen->MigrateSimModels();
    }
    schematic->LoadVariants();
    sheets.CheckForMissingSymbolInstances(project.GetProjectName());
    return schematic;
}

/// Like SPICE_GENERATOR::ItemPins(), a model pin is only connected when it maps to a pin the
/// symbol actually has: Sim.Pins can leave one unmapped, and a model with more terminals than the
/// symbol (a BJT's substrate) gets default numbers the symbol may not have.
ComponentSimNode _pinNode(const SIM_MODEL_PIN& pin, const std::set<wxString>& symbolPins,
                          const std::string& reference) {
    if (!symbolPins.contains(pin.symbolPinNumber)) {
        // Still a node inside the model, just not one any pad connects to.
        return {ComponentSimNode::Kind::Internal, reference + ".nc." + pin.modelPinName};
    }
    return {ComponentSimNode::Kind::Pin, _utf8(pin.symbolPinNumber)};
}

/// The library holding `model`'s base model, with the path SIM_LIB_MGR loaded it from.
std::pair<const SIM_LIBRARY*, wxString> _sourceLibrary(const SIM_LIB_MGR& libraries, const SIM_MODEL& model) {
    const SIM_MODEL* base = model.GetBaseModel();
    if (!base) return {nullptr, wxString()};
    for (const auto& [path, library] : libraries.GetLibraries()) {
        for (const SIM_LIBRARY::MODEL& candidate : library.get().GetModels()) {
            if (&candidate.model == base) return {&library.get(), path};
        }
    }
    return {nullptr, wxString()};
}

/// Expands a resolved model into `out.elements`; false (with out.message set) if it can't be.
bool _expandModel(const SIM_LIB_MGR& libraries, const SIM_MODEL& model, const std::set<wxString>& symbolPins,
                  ComponentSimModel& out) {
    std::vector<ComponentSimNode> nodes;
    for (const SIM_MODEL_PIN& pin : model.GetPins()) {
        nodes.push_back(_pinNode(pin, symbolPins, out.reference));
    }

    if (const auto* subcircuit = dynamic_cast<const SIM_MODEL_SUBCKT*>(&model)) {
        const SIM_LIBRARY* library = _sourceLibrary(libraries, model).first;
        // SPICE scoping: a library subcircuit can instantiate any other subcircuit its library
        // (including files it .includes) defines.
        SubcircuitLookup lookup = [library](const std::string& name) -> std::optional<std::string> {
            if (!library) return std::nullopt;
            if (const auto* nested = dynamic_cast<const SIM_MODEL_SUBCKT*>(library->FindModel(name))) {
                return nested->GetSpiceCode();
            }
            return std::nullopt;
        };
        std::string error;
        std::optional<std::vector<ComponentSimElement>> elements =
                expandSubcircuit(subcircuit->GetSpiceCode(), out.reference, nodes, lookup, error);
        if (!elements) {
            out.message = error;
            return false;
        }
        out.elements = std::move(*elements);
        return true;
    }

    // Every other model is a single SPICE element: the letter KiCad's netlister emits for it.
    ComponentSimElement element;
    element.name = out.reference;
    element.nodes = std::move(nodes);
    if (model.GetType() == SIM_MODEL::TYPE::RAWSPICE) {
        const std::string type = model.GetParam(static_cast<int>(SIM_MODEL_RAW_SPICE::SPICE_PARAM::TYPE)).value;
        element.kind = type.empty() ? 0 : static_cast<char>(std::toupper(static_cast<unsigned char>(type[0])));
        element.value = model.GetParam(static_cast<int>(SIM_MODEL_RAW_SPICE::SPICE_PARAM::MODEL)).value;
    } else {
        const std::string itemType = model.GetSpiceInfo().itemType;
        element.kind = itemType.empty() ? 0 : itemType[0];
        // Ideal and behavioral R/L/C keep their value (or expression) in their one principal
        // parameter; everything else is described by its model.
        if ((element.kind == 'R' || element.kind == 'C' || element.kind == 'L') && model.GetParamCount() > 0) {
            element.value = model.GetParam(0).value;
        } else {
            element.value = out.modelName.empty() ? _utf8(model.GetDeviceInfo().description) : out.modelName;
        }
    }
    if (element.kind == 0) {
        out.message = "KiCad has no SPICE element type for this " + out.deviceType + " model";
        return false;
    }
    out.elements.push_back(std::move(element));
    return true;
}

void _resolveModel(SIM_LIB_MGR& libraries, const SCH_SHEET_PATH& sheet, SCH_SYMBOL& symbol,
                   const wxString& variant, ComponentSimModel& out) {
    MessageReporter reporter;
    try {
        const SIM_LIBRARY::MODEL resolved = libraries.CreateModel(&sheet, symbol, /* aResolve = */ true,
                                                                  /* aDepth = */ 0, variant, reporter);
        const SIM_MODEL& model = resolved.model;
        out.message = reporter.messages;
        if (reporter.HasMessageOfSeverity(RPT_SEVERITY_ERROR)) {
            out.status = ComponentSimModelStatus::Error;
            return;
        }
        if (model.GetType() == SIM_MODEL::TYPE::NONE) {
            out.status = ComponentSimModelStatus::NoModel;
            return;
        }
        if (!model.IsEnabled()) {
            // A legacy (KiCad 7) Sim.Enable=0 field, which predates "Exclude from simulation".
            out.status = ComponentSimModelStatus::ExcludedFromSimulation;
            return;
        }

        out.deviceType = model.GetDeviceInfo().fieldValue;
        if (model.GetBaseModel()) {
            out.modelName = resolved.name;
            out.libraryPath = _utf8(_sourceLibrary(libraries, model).second);
        }
        std::set<wxString> symbolPins;
        for (const SCH_PIN* pin : symbol.GetAllLibPins()) {
            symbolPins.insert(pin->GetNumber());
        }
        out.status = _expandModel(libraries, model, symbolPins, out) ? ComponentSimModelStatus::Resolved
                                                                     : ComponentSimModelStatus::Error;
    } catch (const IO_ERROR& exception) {
        out.status = ComponentSimModelStatus::Error;
        out.message = _utf8(exception.What());
    } catch (const std::exception& exception) {
        out.status = ComponentSimModelStatus::Error;
        out.message = exception.what();
    }
}

} // namespace

SchematicSimModels readSchematicSimModels(PROJECT& project, const std::string& rootPath) {
    SchematicSimModels result;
    SchematicPtr schematic = _loadSchematic(project, rootPath, result.error);
    if (!schematic) return result;

    // One manager for the whole schematic, so each model library is parsed once. Mirrors
    // NETLIST_EXPORTER_SPICE's setup: embedded model files resolve through the schematic.
    SIM_LIB_MGR libraries(&project);
    libraries.SetFilesStack({schematic->GetEmbeddedFiles()});
    const wxString variant = schematic->GetCurrentVariant();

    std::set<wxString> seenReferences;
    for (const SCH_SHEET_PATH& sheet : schematic->BuildSheetListSortedByPageNumbers()) {
        for (SCH_ITEM* item : sheet.LastScreen()->Items().OfType(SCH_SYMBOL_T)) {
            auto* symbol = static_cast<SCH_SYMBOL*>(item);
            const wxString reference = symbol->GetRef(&sheet);
            // '#' marks power symbols and other virtual parts, which never become netlist items. A
            // multi-unit part is one component however many of its units are placed.
            if (reference.IsEmpty() || reference[0] == '#' || !seenReferences.insert(reference).second) {
                continue;
            }

            ComponentSimModel model;
            model.reference = _utf8(reference);
            if (symbol->ResolveExcludedFromSim(&sheet, variant)) {
                model.status = ComponentSimModelStatus::ExcludedFromSimulation;
            } else {
                _resolveModel(libraries, sheet, *symbol, variant, model);
            }
            result.models.push_back(std::move(model));
        }
    }

    result.ok = true;
    return result;
}

} // namespace libkicad::detail
