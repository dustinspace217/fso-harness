"""Which renderer actually came up, judged from the libraries the engine process mapped.

WHY maps and not the log: release builds write no log, and asking for -vulkan is not proof that
Vulkan came up, so the reliable evidence is what the process loaded. The harness forces software GL
for OpenGL runs, which goes through Mesa (libgallium / libGLX_mesa); a Vulkan run maps a Vulkan
driver (NVIDIA: libnvidia-glvkspirv; Mesa: libvulkan_<driver>) and no Mesa GL (both checked against
real engine runs on 2026-10-12 for NVIDIA and Mesa GL; Mesa Vulkan names from /usr/lib64).
"""
import re

_VK = re.compile(r"(libnvidia-glvkspirv\.so|libvulkan_[a-z0-9_]+\.so)")
_GL = re.compile(r"(libgallium[-0-9.]*\.so|libGLX_mesa\.so)")
_ANY = re.compile(r"(libgallium[-0-9.]*\.so|libGLX_[a-z]+\.so|libnvidia-gl[a-z]+\.so|"
                  r"libvulkan_[a-z0-9_]+\.so|libvulkan\.so)")


def classify(maps_text: str) -> dict:
    """Classify a /proc/<pid>/maps dump.

    Returns {"renderer": "vulkan" | "opengl" | "unknown", "libraries": [sorted library names]}.
    """
    libs = sorted(set(_ANY.findall(maps_text)))
    has_gl = bool(_GL.search(maps_text))
    has_vk = bool(_VK.search(maps_text))
    if has_gl:
        kind = "opengl"
    elif has_vk:
        kind = "vulkan"
    else:
        kind = "unknown"
    return {"renderer": kind, "libraries": libs}


def read_maps(pid: int) -> str:
    """Return /proc/<pid>/maps text, or "" if the process is gone or unreadable."""
    try:
        with open(f"/proc/{pid}/maps") as f:
            return f.read()
    except OSError:
        return ""
