"""Which renderer actually came up, judged from the libraries the engine process mapped.

WHY maps and not the log: release builds write no log, and asking for -vulkan is not proof that
Vulkan came up, so the reliable evidence is what the process loaded. An OpenGL run on Xvfb is
software GL through Mesa (libgallium / libGLX_mesa); on Weston it is the GPU's OpenGL (NVIDIA:
libGLX_nvidia, checked 2026-11-20; Mesa hardware drivers also map libgallium); a Vulkan run
maps a Vulkan
driver (NVIDIA: libnvidia-glvkspirv; Mesa: libvulkan_<driver>) and no Mesa GL (both checked against
real engine runs on 2026-10-12 for NVIDIA and Mesa GL; Mesa Vulkan names from /usr/lib64).

On Windows the evidence is the module list (platform.procs.loaded_libraries), read the same
way. Checked there: Mesa's software OpenGL, libgallium_wgl.dll (VM runs, 2026-11-16). NOT yet
checked on a machine with a graphics card: the vendors' OpenGL drivers (nvoglv64.dll,
atio6axx.dll, ig*icd64.dll) and Vulkan. NVIDIA's nvoglv64.dll serves OpenGL and Vulkan alike,
so the one Vulkan-only name is the loader, vulkan-1.dll; whether an OpenGL run ever loads it
is unmeasured (plan B, "Unmeasured").
"""
import re

_VK = re.compile(r"(libnvidia-glvkspirv\.so|libvulkan_[a-z0-9_]+\.so)")
_GL = re.compile(r"(libgallium[-0-9.]*\.so|libGLX_mesa\.so)")
# NVIDIA's own OpenGL (a session on Weston). A Vulkan run on NVIDIA maps libGLX_nvidia too
# (tests/test_renderer.py VK_MAPS), so this decides only after the Vulkan check.
_NV_GL = re.compile(r"(libGLX_nvidia\.so)")
_ANY = re.compile(r"(libgallium[-0-9.]*\.so|libGLX_[a-z]+\.so|libnvidia-gl[a-z]+\.so|"
                  r"libvulkan_[a-z0-9_]+\.so|libvulkan\.so)")
# Windows module names (file names are case-insensitive there).
_WIN_MESA_GL = re.compile(r"(libgallium_wgl\.dll)", re.IGNORECASE)
_WIN_VENDOR_GL = re.compile(r"(nvoglv64\.dll|atio6axx\.dll|ig[0-9a-z]*icd64\.dll)", re.IGNORECASE)
_WIN_VK = re.compile(r"(vulkan-1\.dll)", re.IGNORECASE)


def classify(maps_text: str) -> dict:
    """Classify what a process loaded: a /proc/<pid>/maps dump (Linux) or a list of module
    file names (Windows), as platform.procs.loaded_libraries returns it.

    Returns {"renderer": "vulkan" | "opengl" | "unknown", "libraries": [sorted library names]}.
    The same three words on every OS: callers compare the word with the renderer that was
    asked for. Mesa's OpenGL decides first, as before (it is what a Vulkan request falls back
    to); then a Vulkan driver or, on Windows, the Vulkan loader; then a vendor's OpenGL driver.
    """
    windows = [_WIN_MESA_GL, _WIN_VENDOR_GL, _WIN_VK]
    libs = sorted(set(_ANY.findall(maps_text))
                  | {name.lower() for pattern in windows for name in pattern.findall(maps_text)})
    has_gl = bool(_GL.search(maps_text) or _WIN_MESA_GL.search(maps_text))
    has_vk = bool(_VK.search(maps_text) or _WIN_VK.search(maps_text))
    if has_gl:
        kind = "opengl"
    elif has_vk:
        kind = "vulkan"
    elif _WIN_VENDOR_GL.search(maps_text) or _NV_GL.search(maps_text):
        kind = "opengl"
    else:
        kind = "unknown"
    return {"renderer": kind, "libraries": libs}
