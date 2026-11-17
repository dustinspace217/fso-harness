"""The Win32 calls the win_* modules make, each with its ctypes argument and result types, in
one place (cross-platform Plan B).

WHY one module: ctypes assumes every argument and result is a 32-bit int unless told
otherwise. A handle is pointer-sized, so on 64-bit Windows an undeclared call cuts it in half
and fails only sometimes. Declaring each call once, here, means no caller can forget.

WHY functions that return the library and not module-level objects: ctypes.WinDLL exists only
on Windows, and every win_* module must import on Linux (the parity test loads them there,
and their logic is unit-tested there with these functions replaced by fakes). Nothing in
this file touches Windows until kernel32(), psapi(), user32() or gdi32() is called.

The plain ctypes types below stand in for ctypes.wintypes, so the structures can be defined
on any OS.
"""
import ctypes
import functools

HANDLE = ctypes.c_void_p
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int
WORD = ctypes.c_uint16
SIZE_T = ctypes.c_size_t
INT64 = ctypes.c_int64
UINT64 = ctypes.c_uint64

# Access rights for OpenProcess (WinNT.h) and what they allow here.
SYNCHRONIZE = 0x00100000                       # WaitForSingleObject on the process
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000     # exit state, times, job membership
PROCESS_QUERY_INFORMATION = 0x0400             # with VM_READ: the module list
PROCESS_VM_READ = 0x0010
JOB_OBJECT_QUERY = 0x0004                      # OpenJobObjectW: ask about a job
JOB_OBJECT_TERMINATE = 0x0008                  # OpenJobObjectW: end every process in it

ERROR_ALREADY_EXISTS = 183                     # CreateJobObjectW opened an existing job
ERROR_FILE_NOT_FOUND = 2                       # OpenJobObjectW: no job of that name
ERROR_INVALID_PARAMETER = 87                   # OpenProcess: no process has that pid
ERROR_ACCESS_DENIED = 5                        # OpenJobObjectW: it exists; not ours to open
HANDLE_FLAG_INHERIT = 0x1                      # SetHandleInformation: child processes get it

# Process creation flags (WinBase.h).
CREATE_SUSPENDED = 0x00000004                  # start stopped, so it can join a job first
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400        # the environment block is UTF-16
CREATE_NO_WINDOW = 0x08000000                  # a console program gets no console window
STARTF_USESTDHANDLES = 0x00000100              # STARTUPINFOW carries stdin/stdout/stderr
EXTENDED_STARTUPINFO_PRESENT = 0x00080000      # the startup info is a STARTUPINFOEXW
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002   # the attribute: "inherit only these handles"
RESUME_FAILED = 0xFFFFFFFF                     # ResumeThread's (DWORD)-1

# Job Object limits (WinNT.h). KILL_ON_JOB_CLOSE is set for a build's job only (win_scope.run)
# and never for a session's; tests assert both.
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
# Information classes for Set/QueryInformationJobObject (JOBOBJECTINFOCLASS).
JOB_BASIC_ACCOUNTING_INFORMATION = 1
JOB_EXTENDED_LIMIT_INFORMATION = 9
JOB_CPU_RATE_CONTROL_INFORMATION = 15

WAIT_OBJECT_0, WAIT_TIMEOUT = 0x00000000, 0x00000102   # WaitForSingleObject results
GENERIC_ALL = 0x10000000                       # CreateDesktopW / OpenDesktopW: every right
UOI_NAME = 2                                   # GetUserObjectInformationW: the object's name
WM_CLOSE = 0x0010                              # "please close", as the window's X button sends
PW_CLIENTONLY = 0x1                            # PrintWindow: the client area, no frame
PW_RENDERFULLCONTENT = 0x2                     # PrintWindow: include GPU-drawn content
LIST_MODULES_ALL = 0x03                        # EnumProcessModulesEx: 32- and 64-bit modules


class FILETIME(ctypes.Structure):
    """A Windows time stamp: 100-nanosecond intervals since 1601, in two 32-bit halves."""
    _fields_ = [("low", DWORD), ("high", DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """The basic limits of a job (WinNT.h); LimitFlags says which fields count."""
    _fields_ = [("PerProcessUserTimeLimit", INT64), ("PerJobUserTimeLimit", INT64),
                ("LimitFlags", DWORD), ("MinimumWorkingSetSize", SIZE_T),
                ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", DWORD),
                ("Affinity", SIZE_T), ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """Basic limits plus the memory limits (144 bytes on 64-bit Windows)."""
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", UINT64 * 6), ("ProcessMemoryLimit", SIZE_T),
                ("JobMemoryLimit", SIZE_T), ("PeakProcessMemoryUsed", SIZE_T),
                ("PeakJobMemoryUsed", SIZE_T)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    """What a job has used so far; ActiveProcesses is how many processes are in it now."""
    _fields_ = [("TotalUserTime", INT64), ("TotalKernelTime", INT64),
                ("ThisPeriodTotalUserTime", INT64), ("ThisPeriodTotalKernelTime", INT64),
                ("TotalPageFaultCount", DWORD), ("TotalProcesses", DWORD),
                ("ActiveProcesses", DWORD), ("TotalTerminatedProcesses", DWORD)]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    """A job's CPU cap. CpuRate is in hundredths of a percent of ALL processors (10000 is the
    whole machine). The real structure has a union here; only the rate member is used."""
    _fields_ = [("ControlFlags", DWORD), ("CpuRate", DWORD)]


class STARTUPINFOW(ctypes.Structure):
    """How a new process starts: lpDesktop names its desktop, the hStd* fields its streams."""
    _fields_ = [("cb", DWORD), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
                ("lpTitle", ctypes.c_wchar_p), ("dwX", DWORD), ("dwY", DWORD),
                ("dwXSize", DWORD), ("dwYSize", DWORD), ("dwXCountChars", DWORD),
                ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
                ("wShowWindow", WORD), ("cbReserved2", WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    """STARTUPINFOW plus an attribute list, which is where the list of handles the new
    process may inherit goes."""
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    """What CreateProcessW hands back: handles to the new process and its first thread."""
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD),
                ("dwThreadId", DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    """The machine's memory right now; ullAvailPhys is physical memory free to use (bytes)."""
    _fields_ = [("dwLength", DWORD), ("dwMemoryLoad", DWORD), ("ullTotalPhys", UINT64),
                ("ullAvailPhys", UINT64), ("ullTotalPageFile", UINT64),
                ("ullAvailPageFile", UINT64), ("ullTotalVirtual", UINT64),
                ("ullAvailVirtual", UINT64), ("ullAvailExtendedVirtual", UINT64)]


class RECT(ctypes.Structure):
    """A rectangle in pixels."""
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long),
                ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    """The size and pixel format GetDIBits is asked to deliver (40 bytes)."""
    _fields_ = [("biSize", DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", WORD), ("biBitCount", WORD), ("biCompression", DWORD),
                ("biSizeImage", DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", DWORD),
                ("biClrImportant", DWORD)]


def clear_last_error() -> None:
    """Forget the error code of earlier calls on this thread, for a call whose error code
    is read even when it succeeds (CreateJobObjectW: "already exists"). ctypes has the
    setter on Windows only; elsewhere (this module's tests) there is nothing to forget."""
    setter = getattr(ctypes, "set_last_error", None)
    if setter is not None:
        setter(0)


def last_error() -> int:
    """The Windows error code of the last failed call on this thread (GetLastError as ctypes
    kept it; the libraries below are loaded with use_last_error)."""
    return ctypes.get_last_error()


@functools.cache
def kernel32():
    """kernel32.dll with the calls below declared. Loaded once per process."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    times = ctypes.POINTER(FILETIME)
    for fn, result, arguments in (
            (k.OpenProcess, HANDLE, [DWORD, BOOL, DWORD]),
            (k.CloseHandle, BOOL, [HANDLE]),
            (k.WaitForSingleObject, DWORD, [HANDLE, DWORD]),
            (k.GetExitCodeProcess, BOOL, [HANDLE, ctypes.POINTER(DWORD)]),
            (k.GetProcessTimes, BOOL, [HANDLE, times, times, times, times]),
            (k.OpenJobObjectW, HANDLE, [DWORD, BOOL, ctypes.c_wchar_p]),
            (k.IsProcessInJob, BOOL, [HANDLE, HANDLE, ctypes.POINTER(BOOL)]),
            (k.CreateJobObjectW, HANDLE, [ctypes.c_void_p, ctypes.c_wchar_p]),
            (k.SetInformationJobObject, BOOL, [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD]),
            (k.QueryInformationJobObject, BOOL, [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD,
                                                 ctypes.POINTER(DWORD)]),
            (k.AssignProcessToJobObject, BOOL, [HANDLE, HANDLE]),
            (k.TerminateJobObject, BOOL, [HANDLE, ctypes.c_uint]),
            (k.SetHandleInformation, BOOL, [HANDLE, DWORD, DWORD]),
            (k.CreateProcessW, BOOL, [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                      ctypes.c_void_p, BOOL, DWORD, ctypes.c_void_p,
                                      ctypes.c_wchar_p, ctypes.POINTER(STARTUPINFOW),
                                      ctypes.POINTER(PROCESS_INFORMATION)]),
            (k.InitializeProcThreadAttributeList, BOOL, [ctypes.c_void_p, DWORD, DWORD,
                                                         ctypes.POINTER(SIZE_T)]),
            (k.UpdateProcThreadAttribute, BOOL, [ctypes.c_void_p, DWORD, SIZE_T,
                                                 ctypes.c_void_p, SIZE_T, ctypes.c_void_p,
                                                 ctypes.c_void_p]),
            (k.DeleteProcThreadAttributeList, None, [ctypes.c_void_p]),
            (k.ResumeThread, DWORD, [HANDLE]),
            (k.TerminateProcess, BOOL, [HANDLE, ctypes.c_uint]),
            (k.GlobalMemoryStatusEx, BOOL, [ctypes.POINTER(MEMORYSTATUSEX)])):
        fn.restype, fn.argtypes = result, arguments
    return k


@functools.cache
def psapi():
    """psapi.dll (process status) with the two module-list calls declared."""
    p = ctypes.WinDLL("psapi", use_last_error=True)
    p.EnumProcessModulesEx.restype = BOOL
    p.EnumProcessModulesEx.argtypes = [HANDLE, ctypes.POINTER(HANDLE), DWORD,
                                       ctypes.POINTER(DWORD), DWORD]
    p.GetModuleFileNameExW.restype = DWORD
    p.GetModuleFileNameExW.argtypes = [HANDLE, HANDLE, ctypes.c_wchar_p, DWORD]
    return p


@functools.cache
def user32():
    """user32.dll (windows and desktops) with the calls below declared."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    wide = ctypes.c_wchar_p
    for fn, result, arguments in (
            (u.CreateDesktopW, HANDLE, [wide, wide, ctypes.c_void_p, DWORD, DWORD,
                                        ctypes.c_void_p]),
            (u.OpenDesktopW, HANDLE, [wide, DWORD, BOOL, DWORD]),
            (u.CloseDesktop, BOOL, [HANDLE]),
            (u.SetThreadDesktop, BOOL, [HANDLE]),
            (u.GetProcessWindowStation, HANDLE, []),
            (u.GetUserObjectInformationW, BOOL, [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD,
                                                 ctypes.POINTER(DWORD)]),
            (u.EnumWindows, BOOL, [ctypes.c_void_p, ctypes.c_void_p]),
            (u.GetWindowThreadProcessId, DWORD, [HANDLE, ctypes.POINTER(DWORD)]),
            (u.IsWindowVisible, BOOL, [HANDLE]),
            (u.GetClientRect, BOOL, [HANDLE, ctypes.POINTER(RECT)]),
            (u.GetDC, HANDLE, [HANDLE]),
            (u.ReleaseDC, ctypes.c_int, [HANDLE, HANDLE]),
            (u.PrintWindow, BOOL, [HANDLE, HANDLE, ctypes.c_uint]),
            (u.PostMessageW, BOOL, [HANDLE, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t])):
        fn.restype, fn.argtypes = result, arguments
    return u


@functools.cache
def gdi32():
    """gdi32.dll (drawing) with the calls a window capture makes declared."""
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    for fn, result, arguments in (
            (g.CreateCompatibleDC, HANDLE, [HANDLE]),
            (g.CreateCompatibleBitmap, HANDLE, [HANDLE, ctypes.c_int, ctypes.c_int]),
            (g.SelectObject, HANDLE, [HANDLE, HANDLE]),
            (g.GetDIBits, ctypes.c_int, [HANDLE, HANDLE, ctypes.c_uint, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER),
                                         ctypes.c_uint]),
            (g.DeleteObject, BOOL, [HANDLE]),
            (g.DeleteDC, BOOL, [HANDLE])):
        fn.restype, fn.argtypes = result, arguments
    return g


def enum_callback(fn):
    """Wrap fn(hwnd, param) -> bool as the callback EnumWindows takes. WINFUNCTYPE exists only
    on Windows, so it is reached here, at call time."""
    return ctypes.WINFUNCTYPE(BOOL, HANDLE, ctypes.c_void_p)(fn)
