"""Open Image Denoise bridge for Blender builds without an embedded denoiser."""
import ctypes as C
import subprocess
import sys
from pathlib import Path

import numpy as np


def prepare_passes(scene, output):
    scene.cycles.use_denoising = False
    scene.view_layers[0].cycles.denoising_store_passes = True
    scene.use_nodes = True
    layers = scene.node_tree.nodes.new('CompositorNodeRLayers')
    layers.layer = scene.view_layers[0].name
    files = scene.node_tree.nodes.new('CompositorNodeOutputFile')
    files.base_path = str(output)
    files.format.file_format = 'OPEN_EXR'
    files.format.color_depth = '32'
    files.format.color_mode = 'RGBA'
    files.file_slots.clear()
    for name, source in (('color', 'Image'), ('albedo', 'Denoising Albedo'), ('normal', 'Denoising Normal')):
        files.file_slots.new(name)
        files.file_slots[-1].path = name + '_'
        scene.node_tree.links.new(layers.outputs[source], files.inputs[name])


def denoise(scene, output, library):
    import bpy
    # Read linear, full-precision passes; PNG/tone-mapped pixels are unsuitable here.
    images, buffers = {}, {}
    for name in ('color', 'albedo', 'normal'):
        path, = output.glob(name + '_*.exr')
        image = bpy.data.images.load(str(path), check_existing=False)
        image.colorspace_settings.name = 'Non-Color'
        width, height = image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        images[name] = pixels.reshape(height, width, 4)
        buffers[name] = np.ascontiguousarray(images[name][:, :, :3])
        bpy.data.images.remove(image)
    # Keep OIDN's native runtime separate from Blender's loaded libraries.
    inputs, result = output / 'denoise-input.npz', output / 'denoise-output.npy'
    np.savez(inputs, **buffers)
    subprocess.run([sys.executable, str(Path(__file__).resolve()), library, str(inputs), str(result)],
                   check=True, timeout=30)
    buffers['output'] = np.load(result)
    rgba = images['color']
    rgba[:, :, :3] = buffers['output']
    rgba[rgba[:, :, 3] == 0, :3] = 0
    image = bpy.data.images.new('Denoised preview', width=width, height=height, alpha=True, float_buffer=True)
    image.alpha_mode = 'PREMUL'
    image.pixels.foreach_set(rgba.ravel())
    image.save_render(str(output / 'preview.png'), scene=scene)
    bpy.data.images.remove(image)


def filter_buffers(buffers, library):
    height, width, _ = buffers['color'].shape
    buffers['output'] = np.empty_like(buffers['color'])
    lib = C.CDLL(library)
    ptr, size, string = C.c_void_p, C.c_size_t, C.c_char_p
    signatures = {
        'oidnNewDevice': (ptr, [C.c_int]),
        'oidnSetDeviceInt': (None, [ptr, string, C.c_int]),
        'oidnCommitDevice': (None, [ptr]),
        'oidnNewFilter': (ptr, [ptr, string]),
        'oidnSetSharedFilterImage': (None, [ptr, string, ptr, C.c_int, size, size, size, size, size]),
        'oidnSetFilterBool': (None, [ptr, string, C.c_bool]),
        'oidnCommitFilter': (None, [ptr]),
        'oidnExecuteFilter': (None, [ptr]),
        'oidnGetDeviceError': (C.c_int, [ptr, C.POINTER(string)]),
        'oidnReleaseFilter': (None, [ptr]),
        'oidnReleaseDevice': (None, [ptr]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments

    def check(device):
        message = string()
        if lib.oidnGetDeviceError(device, C.byref(message)):
            raise RuntimeError('Open Image Denoise: ' + message.value.decode())

    device, filter_ = lib.oidnNewDevice(1), None  # CPU; shared host buffers.
    try:
        check(device)
        lib.oidnSetDeviceInt(device, b'numThreads', 8)
        lib.oidnCommitDevice(device)
        check(device)
        filter_ = lib.oidnNewFilter(device, b'RT')
        check(device)
        for name, pixels in buffers.items():
            lib.oidnSetSharedFilterImage(filter_, name.encode(), pixels.ctypes.data,
                                         3, width, height, 0, 0, 0)  # FLOAT3
        lib.oidnSetFilterBool(filter_, b'hdr', True)
        lib.oidnCommitFilter(filter_)
        check(device)
        lib.oidnExecuteFilter(filter_)
        check(device)
    finally:
        if filter_:
            lib.oidnReleaseFilter(filter_)
        if device:
            lib.oidnReleaseDevice(device)
    return buffers['output']


if __name__ == '__main__':
    library, source, target = sys.argv[1:]
    with np.load(source) as data:
        result = filter_buffers(dict(data), library)
    np.save(target, result)
