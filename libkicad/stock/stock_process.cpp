#include "stock_process.hpp"

#include <cerrno>
#include <cstring>
#include <system_error>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
extern char** environ;
#endif

namespace libkicad::detail {

namespace {

std::filesystem::path _fromUtf8(const std::string& text) {
    return std::filesystem::path(std::u8string(text.begin(), text.end()));
}

std::string _utf8(const std::filesystem::path& path) {
    const std::u8string text = path.u8string();
    return std::string(text.begin(), text.end());
}

#if defined(_WIN32)
// Quotes one argument so the child's CommandLineToArgvW reproduces it exactly.
std::wstring _quote(const std::wstring& argument) {
    if (!argument.empty() && argument.find_first_of(L" \t\n\v\"") == std::wstring::npos) {
        return argument;
    }
    std::wstring quoted = L"\"";
    for (auto it = argument.begin();; ++it) {
        std::size_t backslashes = 0;
        while (it != argument.end() && *it == L'\\') {
            ++it;
            ++backslashes;
        }
        if (it == argument.end()) {
            quoted.append(backslashes * 2, L'\\');
            break;
        }
        quoted.append(*it == L'"' ? backslashes * 2 + 1 : backslashes, L'\\');
        quoted.push_back(*it);
    }
    quoted.push_back(L'"');
    return quoted;
}

std::string _lastError() {
    return std::system_category().message(static_cast<int>(GetLastError()));
}
#endif

} // namespace

int runProcess(const std::filesystem::path& executable, const std::vector<std::string>& arguments,
               std::string& error) {
#if defined(_WIN32)
    std::wstring commandLine = _quote(executable.wstring());
    for (const std::string& argument : arguments) {
        commandLine += L' ';
        commandLine += _quote(_fromUtf8(argument).wstring());
    }

    // One NUL handle per stream: a child that redirects or closes one of its standard streams (as
    // Python's os.dup2 does) must not invalidate the others along with it.
    SECURITY_ATTRIBUTES inheritable{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    HANDLE streams[3];
    for (HANDLE& stream : streams) {
        stream = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &inheritable,
                             OPEN_EXISTING, 0, nullptr);
    }
    const auto closeStreams = [&streams] {
        for (HANDLE stream : streams) {
            if (stream != INVALID_HANDLE_VALUE) CloseHandle(stream);
        }
    };
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = streams[0];
    startup.hStdOutput = streams[1];
    startup.hStdError = streams[2];
    PROCESS_INFORMATION process{};
    const BOOL started = CreateProcessW(executable.c_str(), commandLine.data(), nullptr, nullptr, TRUE,
                                        CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process);
    if (!started) {
        error = "Could not run \"" + _utf8(executable) + "\": " + _lastError();
        closeStreams();
        return -1;
    }
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD status = 1;
    GetExitCodeProcess(process.hProcess, &status);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    closeStreams();
    return static_cast<int>(status);
#else
    std::vector<std::string> owned;
    owned.push_back(executable.string());
    owned.insert(owned.end(), arguments.begin(), arguments.end());
    std::vector<char*> argv;
    for (std::string& argument : owned) argv.push_back(argument.data());
    argv.push_back(nullptr);

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0);
    posix_spawn_file_actions_addopen(&actions, 1, "/dev/null", O_WRONLY, 0);
    posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0);
    pid_t pid = 0;
    const int spawned = posix_spawn(&pid, owned.front().c_str(), &actions, nullptr, argv.data(), environ);
    posix_spawn_file_actions_destroy(&actions);
    if (spawned != 0) {
        error = "Could not run \"" + owned.front() + "\": " + std::strerror(spawned);
        return -1;
    }
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            error = std::string("waitpid failed: ") + std::strerror(errno);
            return -1;
        }
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : 0);
#endif
}

} // namespace libkicad::detail
