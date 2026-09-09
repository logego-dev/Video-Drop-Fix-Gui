# Third-Party Notices

FixDrops is an independent application that integrates third-party tools.

The MIT license in the repository root applies to the FixDrops project code.
It does not replace the licenses of third-party executables, libraries,
model weights, or other assets.

## rife-ncnn-vulkan

Project:

https://github.com/nihui/rife-ncnn-vulkan

Copyright notice supplied by the upstream project:

> Copyright (c) 2020 nihui

The supplied upstream license is MIT.

When redistributing the corresponding software, retain its complete
copyright and license notice. A copy must accompany the distributed
component, for example at:

`third_party/rife-ncnn-vulkan/LICENSE`

Do not remove other notices included in the upstream distribution.

## Model weights

Model weights are separate assets. Do not assume that a wrapper's MIT
license automatically licenses every model distributed alongside it.

For each included model, record its exact source and applicable license.
Preserve all associated notices and satisfy the applicable redistribution
conditions before including it in a release.

## ncnn and other components in the RIFE distribution

Project:

https://github.com/Tencent/ncnn

Preserve the licenses and notices required by the exact upstream
distribution being redistributed.

## FFmpeg

Project:

https://ffmpeg.org/

License information:

https://ffmpeg.org/legal.html

FFmpeg licensing depends on the build configuration and included components.
If an FFmpeg binary is redistributed, comply with the requirements of that
specific build, including applicable source-code distribution obligations.

The FixDrops MIT license does not relicense FFmpeg.

## Python dependencies

The application uses:

- PyAV: https://github.com/PyAV-Org/PyAV
- NumPy: https://github.com/numpy/numpy
- OpenCV: https://github.com/opencv/opencv
- tkinterdnd2: https://github.com/pmgagne/tkinterdnd2

These projects and their bundled components retain their respective licenses.

If distributing a packaged application containing these dependencies,
include the notices and other materials required by the versions actually
shipped.

## Release component inventory

Complete this table for each binary release.

| Component | Exact version / release | Download source | License / notice location |
|---|---|---|---|
| rife-ncnn-vulkan | TO BE FILLED | TO BE FILLED | TO BE FILLED |
| RIFE model weights | TO BE FILLED | TO BE FILLED | TO BE FILLED |
| FFmpeg, if included | TO BE FILLED | TO BE FILLED | TO BE FILLED |
| Other bundled components | TO BE FILLED | TO BE FILLED | TO BE FILLED |