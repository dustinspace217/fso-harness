from fsoctl.renderer import classify

GL_MAPS = """7f00-7f01 r-xp 0 08:01 1 /usr/lib64/libgallium-26.2.3.so
7f02-7f03 r-xp 0 08:01 2 /usr/lib64/libGLX_mesa.so.0.0.0
"""
VK_MAPS = """7f00-7f01 r-xp 0 08:01 1 /usr/lib64/libGLX_nvidia.so.595.71
7f02-7f03 r-xp 0 08:01 2 /usr/lib64/libnvidia-glvkspirv.so.595.71
7f04-7f05 r-xp 0 08:01 3 /usr/lib64/libvulkan.so.1.3.0
"""
FALLBACK_MAPS = VK_MAPS + GL_MAPS


def test_classify_opengl():
    assert classify(GL_MAPS)["renderer"] == "opengl"


def test_classify_vulkan_nvidia():
    r = classify(VK_MAPS)
    assert r["renderer"] == "vulkan"
    assert "libnvidia-glvkspirv.so" in r["libraries"]


def test_classify_mesa_gl_loaded_means_fallback_to_opengl():
    assert classify(FALLBACK_MAPS)["renderer"] == "opengl"


def test_classify_mesa_vulkan_driver():
    maps = "7f00-7f01 r-xp 0 08:01 1 /usr/lib64/libvulkan_radeon.so\n"
    assert classify(maps)["renderer"] == "vulkan"


def test_classify_nothing_known():
    assert classify("")["renderer"] == "unknown"


# Library paths captured from /proc/<pid>/maps of real engine runs (2026-10-12):
# release build, an NVIDIA Vulkan ICD vs Mesa software OpenGL, both on Xvfb.
REAL_VULKAN = "\n".join("7f00-7f01 r-xp 0 08:01 1 " + p for p in (
    "/usr/lib64/libGLX_nvidia.so.600.0.0", "/usr/lib64/libnvidia-glcore.so.600.0.0",
    "/usr/lib64/libnvidia-glvkspirv.so.600.0.0", "/usr/lib64/libvulkan.so.1.3.0"))
REAL_OPENGL = "\n".join("7f00-7f01 r-xp 0 08:01 1 " + p for p in (
    "/usr/lib64/libgallium-26.2.3.so", "/usr/lib64/libGLdispatch.so.0.0.0",
    "/usr/lib64/libGL.so.1.7.0", "/usr/lib64/libGLX_mesa.so.0.0.0"))


def test_classify_real_captures():
    assert classify(REAL_VULKAN)["renderer"] == "vulkan"
    assert classify(REAL_OPENGL)["renderer"] == "opengl"
