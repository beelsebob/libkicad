cmake_minimum_required(VERSION 3.25)

project(libkicad LANGUAGES CXX)

# libkicad deliberately compiles against KiCad's internal C++ API.  Build KiCad first with
# Scripts/build_kicad.sh (or an equivalent Linux KiCad build) and point KICAD_BUILD_ROOT at it.
set(KICAD_SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/submodules/kicad" CACHE PATH
    "KiCad source tree used by libkicad")
set(KICAD_BUILD_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/build/kicad" CACHE PATH
    "Matching KiCad build tree used by libkicad")
set(LIBKICAD_DEPENDENCY_PREFIX "" CACHE PATH
    "Prefix containing wxWidgets, OpenCASCADE, and other KiCad build dependencies")

if(NOT EXISTS "${KICAD_SOURCE_ROOT}/CMakeLists.txt")
    message(FATAL_ERROR "KiCad source is missing at ${KICAD_SOURCE_ROOT}. Run git submodule update --init --recursive.")
endif()

if(NOT EXISTS "${KICAD_BUILD_ROOT}/compile_commands.json")
    message(FATAL_ERROR
        "KiCad build is missing at ${KICAD_BUILD_ROOT}. Run Scripts/build_kicad.sh first, or set KICAD_BUILD_ROOT to a matching KiCad build.")
endif()

if(LIBKICAD_DEPENDENCY_PREFIX)
    list(PREPEND CMAKE_PREFIX_PATH "${LIBKICAD_DEPENDENCY_PREFIX}")
elseif(APPLE AND EXISTS "/opt/homebrew")
    list(PREPEND CMAKE_PREFIX_PATH "/opt/homebrew")
endif()

file(GLOB_RECURSE LIBKICAD_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/libkicad/*.cpp"
    "${CMAKE_CURRENT_SOURCE_DIR}/libkicad/*.cxx")
# libkicad/eeschema is compiled separately -- see libkicad_eeschema below.
list(FILTER LIBKICAD_SOURCES EXCLUDE REGEX "/libkicad/eeschema/")

# Compile settings shared by every translation unit that includes KiCad headers.  pcbnew and
# eeschema sources need different include paths and definitions on top of these, and KiCad's
# headers only build when each sees its own, so the two halves of libkicad are separate targets.
add_library(libkicad_kicad_flags INTERFACE)

add_library(libkicad STATIC ${LIBKICAD_SOURCES})
add_library(libkicad::libkicad ALIAS libkicad)

# Model resolution for Board::componentSimModels() reads schematics through eeschema.
file(GLOB LIBKICAD_EESCHEMA_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/libkicad/eeschema/*.cpp")
add_library(libkicad_eeschema OBJECT ${LIBKICAD_EESCHEMA_SOURCES})
target_sources(libkicad PRIVATE $<TARGET_OBJECTS:libkicad_eeschema>)

# KiCad headers are currently C++20-only.  The public wrapper intentionally has no KiCad
# headers, so it may expose the C++23 std::expected-based API.
set_target_properties(libkicad libkicad_eeschema PROPERTIES
    CXX_STANDARD 20
    CXX_STANDARD_REQUIRED YES
    CXX_EXTENSIONS NO)
set_target_properties(libkicad PROPERTIES OUTPUT_NAME libkicad)
set_source_files_properties("${CMAKE_CURRENT_SOURCE_DIR}/libkicad/libkicad_api.cpp"
    PROPERTIES COMPILE_OPTIONS "-std=c++23")

target_include_directories(libkicad
    PUBLIC
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}>"
        "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/libkicad>"
    PRIVATE
        "${KICAD_SOURCE_ROOT}/pcbnew"
        "${KICAD_SOURCE_ROOT}/pcbnew/dialogs"
        "${KICAD_SOURCE_ROOT}/pcbnew/autorouter"
        "${KICAD_SOURCE_ROOT}/pcbnew/generators"
        "${KICAD_SOURCE_ROOT}/pcbnew/exporters"
        "${KICAD_SOURCE_ROOT}/pcbnew/importers"
        "${KICAD_SOURCE_ROOT}/pcbnew/specctra_import_export"
        "${KICAD_BUILD_ROOT}/pcbnew")
target_compile_definitions(libkicad PRIVATE PCBNEW)
target_link_libraries(libkicad PRIVATE libkicad_kicad_flags)

target_include_directories(libkicad_eeschema PRIVATE
    "${KICAD_SOURCE_ROOT}/eeschema"
    "${KICAD_SOURCE_ROOT}/eeschema/netlist_exporters"
    "${KICAD_BUILD_ROOT}/eeschema")
target_link_libraries(libkicad_eeschema PRIVATE libkicad_kicad_flags)

target_include_directories(libkicad_kicad_flags
    INTERFACE
        "${CMAKE_CURRENT_SOURCE_DIR}/libkicad"
        "${KICAD_SOURCE_ROOT}"
        "${KICAD_SOURCE_ROOT}/include"
        "${KICAD_SOURCE_ROOT}/3d-viewer"
        "${KICAD_SOURCE_ROOT}/3d-viewer/dialogs"
        "${KICAD_SOURCE_ROOT}/common"
        "${KICAD_SOURCE_ROOT}/common/dialogs"
        "${KICAD_SOURCE_ROOT}/utils/idftools"
        "${KICAD_SOURCE_ROOT}/thirdparty/argparse/include"
        "${KICAD_SOURCE_ROOT}/thirdparty/3dxware_sdk/inc"
        "${KICAD_SOURCE_ROOT}/thirdparty/clipper2/Clipper2Lib/include"
        "${KICAD_SOURCE_ROOT}/thirdparty/compoundfilereader"
        "${KICAD_SOURCE_ROOT}/thirdparty/dynamic_bitset"
        "${KICAD_SOURCE_ROOT}/thirdparty/dxflib_qcad"
        "${KICAD_SOURCE_ROOT}/thirdparty/eigen"
        "${KICAD_SOURCE_ROOT}/thirdparty/expected/include"
        "${KICAD_SOURCE_ROOT}/thirdparty/fast_float/include"
        "${KICAD_SOURCE_ROOT}/thirdparty/fmt/include"
        "${KICAD_SOURCE_ROOT}/thirdparty/glad/include"
        "${KICAD_SOURCE_ROOT}/thirdparty/gzip-hpp"
        "${KICAD_SOURCE_ROOT}/thirdparty/json_schema_validator"
        "${KICAD_SOURCE_ROOT}/thirdparty/libcontext"
        "${KICAD_SOURCE_ROOT}/thirdparty/libpopcnt"
        "${KICAD_SOURCE_ROOT}/thirdparty/magic_enum/magic_enum"
        "${KICAD_SOURCE_ROOT}/thirdparty/nanodbc"
        "${KICAD_SOURCE_ROOT}/thirdparty/nanoflann"
        "${KICAD_SOURCE_ROOT}/thirdparty/nanosvg"
        "${KICAD_SOURCE_ROOT}/thirdparty/nlohmann_json"
        "${KICAD_SOURCE_ROOT}/thirdparty/pegtl"
        "${KICAD_SOURCE_ROOT}/thirdparty/picosha2"
        "${KICAD_SOURCE_ROOT}/thirdparty/planegcs"
        "${KICAD_SOURCE_ROOT}/thirdparty/rapidcsv"
        "${KICAD_SOURCE_ROOT}/thirdparty/rectpack2d"
        "${KICAD_SOURCE_ROOT}/thirdparty/rtree"
        "${KICAD_SOURCE_ROOT}/thirdparty/thread-pool"
        "${KICAD_SOURCE_ROOT}/thirdparty/tinyspline_lib"
        "${KICAD_SOURCE_ROOT}/libs/core/include"
        "${KICAD_SOURCE_ROOT}/libs/kimath/include"
        "${KICAD_SOURCE_ROOT}/libs/kinng/include"
        "${KICAD_SOURCE_ROOT}/libs/kiplatform/include"
        "${KICAD_SOURCE_ROOT}/libs/sexpr/include"
        "${KICAD_SOURCE_ROOT}/libs/snap/include"
        "${KICAD_BUILD_ROOT}"
        "${KICAD_BUILD_ROOT}/api/cpp"
        "${KICAD_BUILD_ROOT}/api/cpp/api"
        "${KICAD_BUILD_ROOT}/common"
        "${KICAD_BUILD_ROOT}/include/gal/shaders")

target_compile_definitions(libkicad_kicad_flags INTERFACE
    BOOST_ATOMIC_DYN_LINK BOOST_ATOMIC_NO_LIB
    BOOST_CHARCONV_DYN_LINK BOOST_CHARCONV_NO_LIB
    BOOST_CHRONO_DYN_LINK BOOST_CHRONO_NO_LIB
    BOOST_CONTAINER_DYN_LINK BOOST_CONTAINER_NO_LIB
    BOOST_DATE_TIME_DYN_LINK BOOST_DATE_TIME_NO_LIB
    BOOST_LOCALE_DYN_LINK BOOST_LOCALE_NO_LIB
    BOOST_THREAD_DYN_LINK BOOST_THREAD_NO_LIB
    BS_THREAD_POOL_NATIVE_EXTENSIONS=1
    GLM_FORCE_CTOR_INIT HAVE_STDINT_H JSON_HAS_FILESYSTEM=0
    KIAPI_IMPORTEXPORT=APIIMPORT KICAD_CONFIG_DIR=kicad KICAD_SIGNAL_INTEGRITY
    NANODBC_ENABLE_UNICODE PROTOBUF_USE_DLLS USINGZ WXUSINGDLL
    _FILE_OFFSET_BITS=64 wxDEBUG_LEVEL=0)

if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(arm64|aarch64)$")
    target_compile_definitions(libkicad_kicad_flags INTERFACE KICAD_BUILD_ARCH_ARM64)
elseif(CMAKE_SIZEOF_VOID_P EQUAL 8)
    target_compile_definitions(libkicad_kicad_flags INTERFACE KICAD_BUILD_ARCH_X64)
else()
    target_compile_definitions(libkicad_kicad_flags INTERFACE KICAD_BUILD_ARCH_X86)
endif()

if(APPLE)
    target_compile_definitions(libkicad_kicad_flags INTERFACE GL_SILENCE_DEPRECATION=1 __WXMAC__ __WXOSX__ __WXOSX_COCOA__)
    set(_libkicad_default_prefix "/opt/homebrew")
    if(NOT EXISTS "${_libkicad_default_prefix}")
        set(_libkicad_default_prefix "/usr/local")
    endif()
    if(NOT LIBKICAD_DEPENDENCY_PREFIX)
        set(LIBKICAD_DEPENDENCY_PREFIX "${_libkicad_default_prefix}")
    endif()
    target_include_directories(libkicad_kicad_flags SYSTEM INTERFACE
        "${LIBKICAD_DEPENDENCY_PREFIX}/include"
        "${LIBKICAD_DEPENDENCY_PREFIX}/include/cairo"
        "${LIBKICAD_DEPENDENCY_PREFIX}/include/pixman-1"
        "${LIBKICAD_DEPENDENCY_PREFIX}/include/freetype2"
        "${LIBKICAD_DEPENDENCY_PREFIX}/opt/harfbuzz/include/harfbuzz"
        "${LIBKICAD_DEPENDENCY_PREFIX}/opt/opencascade/include/opencascade"
        "${LIBKICAD_DEPENDENCY_PREFIX}/lib/wx/include/osx_cocoa-unicode-3.2"
        "${LIBKICAD_DEPENDENCY_PREFIX}/include/wx-3.2")

    # libkicad wraps KiCad internals, so the final consumer of this static archive must also link
    # the corresponding KiCad build products.  Xcode gets this list from KiCadLink.xcconfig; make
    # it part of the CMake target's public link interface too.
    set(KICAD_FRAMEWORK_ROOT "${KICAD_BUILD_ROOT}/kicad/KiCad.app/Contents/Frameworks")
    set(KICAD_LINK_LIBRARIES
        "${KICAD_BUILD_ROOT}/pcbnew/CMakeFiles/pcbnew_kiface_objects.dir/api/api_handler_pcb.cpp.o"
        "${KICAD_BUILD_ROOT}/pcbnew/CMakeFiles/pcbnew_kiface_objects.dir/api/api_handler_board.cpp.o"
        "${KICAD_BUILD_ROOT}/pcbnew/CMakeFiles/pcbnew_kiface_objects.dir/api/headless_pcb_context.cpp.o"
        "${KICAD_BUILD_ROOT}/pcbnew/CMakeFiles/pcbnew_kiface_objects.dir/api/pcb_context.cpp.o"
        # GetStandardColors(): STEP export resolves named stackup colors through it.  Without this
        # object, -undefined dynamic_lookup binds it to null and boards with e.g. "FR4 natural"
        # crash in STEP_PCB_MODEL::CreatePCB().
        "${KICAD_BUILD_ROOT}/pcbnew/CMakeFiles/pcbnew_kiface_objects.dir/board_stackup_manager/stackup_predefined_prms.cpp.o"
        # Schematic loading and simulation models (Board::componentSimModels()); see
        # Scripts/build_kicad.sh.  The linker only extracts the eeschema objects that are used.
        "${KICAD_BUILD_ROOT}/eeschema/libeeschema_kiface_objects.a"
        "${KICAD_BUILD_ROOT}/eeschema/navlib/libeeschema_navlib.a"
        "${KICAD_BUILD_ROOT}/common/io/pads/libpads_common.a"
        "${KICAD_BUILD_ROOT}/thirdparty/libwmf/liblibwmf_orcad.a"
        "${KICAD_BUILD_ROOT}/thirdparty/nanodbc/libnanodbc.a"
        "${KICAD_BUILD_ROOT}/thirdparty/tinyspline_lib/libtinyspline_lib.a"
        "${KICAD_BUILD_ROOT}/libs/snap/libsnap.a"
        "${KICAD_BUILD_ROOT}/common/libpcbcommon.a"
        "${KICAD_BUILD_ROOT}/libs/kiplatform/libkiplatform.a"
        "${KICAD_BUILD_ROOT}/common/libcommon.a"
        "${KICAD_BUILD_ROOT}/libs/core/libcore.a"
        "${KICAD_BUILD_ROOT}/thirdparty/planegcs/libplanegcs.a"
        "${KICAD_BUILD_ROOT}/libs/kimath/libkimath.a"
        "${KICAD_BUILD_ROOT}/thirdparty/clipper2/libclipper2.a"
        "${KICAD_BUILD_ROOT}/thirdparty/fmt/libfmt.a"
        "${KICAD_FRAMEWORK_ROOT}/libkigal.dylib"
        "${KICAD_FRAMEWORK_ROOT}/libkicommon.dylib"
        "${KICAD_FRAMEWORK_ROOT}/libkiapi.dylib")
    file(GLOB KICAD_OPENCASCADE_LIBRARIES "${LIBKICAD_DEPENDENCY_PREFIX}/lib/libTK*.dylib")
    list(APPEND KICAD_LINK_LIBRARIES ${KICAD_OPENCASCADE_LIBRARIES})
    foreach(wx_library IN ITEMS wx_osx_cocoau_gl-3.2 wx_osx_cocoau_aui-3.2 wx_osx_cocoau_html-3.2
            wx_osx_cocoau_core-3.2 wx_baseu_net-3.2 wx_baseu-3.2 wx_osx_cocoau_propgrid-3.2
            wx_baseu_xml-3.2 wx_osx_cocoau_stc-3.2 wx_osx_cocoau_richtext-3.2 wx_osx_cocoau_webview-3.2)
        find_library(LIBKICAD_${wx_library} NAMES ${wx_library} PATHS "${LIBKICAD_DEPENDENCY_PREFIX}/lib" NO_DEFAULT_PATH)
        if(LIBKICAD_${wx_library})
            list(APPEND KICAD_LINK_LIBRARIES "${LIBKICAD_${wx_library}}")
        endif()
    endforeach()
    target_link_libraries(libkicad INTERFACE ${KICAD_LINK_LIBRARIES})
    target_link_options(libkicad INTERFACE "-Wl,-undefined,dynamic_lookup")
else()
    list(PREPEND CMAKE_MODULE_PATH "${KICAD_SOURCE_ROOT}/cmake")
    # kiplatform is a static KiCad library.  Its original build links GTK, X11,
    # libsecret, and Poppler privately, so a consumer must repeat those links.
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(GTK3 REQUIRED IMPORTED_TARGET gtk+-3.0)
    pkg_check_modules(X11 REQUIRED IMPORTED_TARGET x11)
    pkg_check_modules(LIBSECRET REQUIRED IMPORTED_TARGET libsecret-1)
    find_package(Poppler REQUIRED COMPONENTS Glib)
    find_package(SPNAV REQUIRED)
    find_package(wxWidgets 3.2 REQUIRED COMPONENTS
        gl aui adv html core net base propgrid xml stc richtext webview)
    find_package(Freetype REQUIRED)
    find_package(HarfBuzz REQUIRED)
    find_package(Fontconfig REQUIRED)
    find_package(libgit2 REQUIRED)
    find_package(Cairo REQUIRED)
    find_package(Pixman REQUIRED)
    # CMake 4.2's FindOpenGL can fail to discover GLU on multiarch Ubuntu even when
    # libglu1-mesa-dev is installed.  Find the link library directly instead.
    find_library(LIBKICAD_GLU_LIBRARY NAMES GLU glu REQUIRED)
    find_package(Protobuf REQUIRED)
    include("${KICAD_SOURCE_ROOT}/cmake/FindOCC.cmake")

    # libkicad reaches into pcbnew's internal C++ API.  KiCad hides those implementation symbols
    # in its loadable _pcbnew.kiface module, so Linux consumers link an archive of its complete
    # object target instead.  Scripts/build_kicad.sh creates that archive after building KiCad.
    set(KICAD_PCBNEW_OBJECTS_ARCHIVE
        "${KICAD_BUILD_ROOT}/pcbnew/libpcbnew_kiface_objects.a")
    set(KICAD_3DSG_LIBRARY
        "${KICAD_BUILD_ROOT}/3d-viewer/3d_cache/sg/libkicad_3dsg.so")
    add_custom_command(
        OUTPUT "${KICAD_PCBNEW_OBJECTS_ARCHIVE}" "${KICAD_3DSG_LIBRARY}"
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/Scripts/build_kicad.sh"
        DEPENDS
            "${CMAKE_CURRENT_SOURCE_DIR}/Scripts/build_kicad.sh"
            "${KICAD_SOURCE_ROOT}/CMakeLists.txt"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        COMMENT "Building KiCad pcbnew objects for libkicad"
        VERBATIM)
    add_custom_target(libkicad_kicad_build DEPENDS
        "${KICAD_PCBNEW_OBJECTS_ARCHIVE}" "${KICAD_3DSG_LIBRARY}")
    add_dependencies(libkicad libkicad_kicad_build)
    file(GLOB_RECURSE KICAD_STATIC_LIBRARIES CONFIGURE_DEPENDS
        "${KICAD_BUILD_ROOT}/*.a")
    list(REMOVE_ITEM KICAD_STATIC_LIBRARIES "${KICAD_PCBNEW_OBJECTS_ARCHIVE}")
    set(KICAD_PCB_COMMON_LIBRARY "${KICAD_BUILD_ROOT}/common/libpcbcommon.a")
    set(KICAD_COMMON_LIBRARY "${KICAD_BUILD_ROOT}/common/libcommon.a")
    list(REMOVE_ITEM KICAD_STATIC_LIBRARIES
        "${KICAD_PCB_COMMON_LIBRARY}"
        "${KICAD_COMMON_LIBRARY}")
    set(KICAD_LINK_LIBRARIES
        "${KICAD_PCBNEW_OBJECTS_ARCHIVE}"
        # Keep KiCad's pcbcommon-before-common ordering: both contain a netlist implementation,
        # and the first archive selected supplies the appropriate PCB variant.
        "${KICAD_PCB_COMMON_LIBRARY}"
        "${KICAD_COMMON_LIBRARY}"
        # KiCad has cycles amongst its remaining static archives.  Do not include common or
        # pcbcommon in this group: doing so allows both netlist implementations to be extracted.
        "-Wl,--start-group"
        ${KICAD_STATIC_LIBRARIES}
        # pnsrouter and the PCB I/O archives can reference code in the kiface object archive.
        "${KICAD_PCBNEW_OBJECTS_ARCHIVE}"
        "-Wl,--end-group"
        "${KICAD_3DSG_LIBRARY}"
        "${KICAD_BUILD_ROOT}/common/gal/libkigal.so"
        "${KICAD_BUILD_ROOT}/common/libkicommon.so"
        "${KICAD_BUILD_ROOT}/api/libkiapi.so")

    target_include_directories(libkicad_kicad_flags SYSTEM INTERFACE
        ${wxWidgets_INCLUDE_DIRS}
        ${FREETYPE_INCLUDE_DIRS}
        ${HarfBuzz_INCLUDE_DIRS}
        ${Fontconfig_INCLUDE_DIRS})
    target_include_directories(libkicad_kicad_flags SYSTEM INTERFACE ${OCC_INCLUDE_DIR})
    target_compile_definitions(libkicad_kicad_flags INTERFACE ${wxWidgets_DEFINITIONS})
    target_link_libraries(libkicad INTERFACE
        ${KICAD_LINK_LIBRARIES}
        ${wxWidgets_LIBRARIES}
        ${OCC_LIBRARIES}
        Freetype::Freetype
        HarfBuzz::HarfBuzz
        Fontconfig::Fontconfig
        ${LIBGIT2_LIBRARIES}
        ${CAIRO_LIBRARIES}
        ${PIXMAN_LIBRARIES}
        PkgConfig::GTK3
        PkgConfig::X11
        PkgConfig::LIBSECRET
        Poppler::Glib
        ${SPNAV_LIBRARIES}
        ${LIBKICAD_GLU_LIBRARY}
        protobuf::libprotobuf)

    # CMake may place the system libraries before the static KiCad archives when composing a
    # consumer's final link line.  GNU ld's default --as-needed then discards (for example)
    # FreeType before libcommon.a introduces its references.
    target_link_options(libkicad INTERFACE -Wl,--no-as-needed)
endif()

target_compile_options(libkicad_kicad_flags INTERFACE
    -Wall -Wsuggest-override -Werror=vla
    -Wimplicit-fallthrough -Werror=return-type -Wshadow -Wsign-compare
    -Wmissing-field-initializers -Wempty-body -Wreorder
    -Wpessimizing-move -Wredundant-move -fPIC -fvisibility=hidden
    -fvisibility-inlines-hidden -ftrivial-auto-var-init=zero
    "-include${KICAD_SOURCE_ROOT}/include/import_export.h")

if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    target_compile_options(libkicad_kicad_flags INTERFACE
        -Winconsistent-missing-override -Wmismatched-tags -fstandalone-debug -Wno-psabi)
endif()

include(GNUInstallDirs)
install(TARGETS libkicad ARCHIVE DESTINATION "${CMAKE_INSTALL_LIBDIR}")
install(FILES libkicad/libkicad.hpp libkicad/libkicad_result.hpp DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/libkicad")
