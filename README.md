# TimelapsePro

Timelapse post-processing for PiSlider + Sony camera sequences.

Reads Sony ARW RAW files together with the XMP sidecars PiSlider writes during
a shoot, and applies the corrections that need rig metadata to do properly:

- **Exposure correction** from `crs:Exposure2012`
- **White balance** — the camera-commanded WB embedded in the RAW
- **Tilt correction** from `ps:Rig_Tilt_Deg`, so a levelled horizon survives a
  slider move that was not perfectly level
- **S-Log3 tone encoding** (optional)
- **Export to ProRes 4444, or to a 16-bit TIFF sequence**

Because the rig records its own tilt per frame, the correction is driven by
measured geometry rather than estimated from image content.

## Why bake the corrections in

PiSlider writes its exposure and white-balance decisions into each sidecar as
Adobe Camera Raw keys (`crs:Exposure2012`, `crs:Temperature`). **Only Adobe
software and Resolve read those.** darktable, RawTherapee and most other editors
ignore the `crs:` namespace entirely — they read XMP sidecars, but only for
metadata like ratings and tags, never for another vendor's develop settings.

So if you do not use Lightroom, those corrections are computed during the shoot
and then silently discarded when you open the frames. Running the sequence
through TimelapsePro applies them to the pixels, which is what makes them
survive into any editor.

## Output formats

### ProRes 4444 (`.mov`)

For editing and grading. One file, 10-bit 4:4:4, Apple vendor tag for QuickTime
compatibility. Needs `ffmpeg`.

### 16-bit TIFF sequence

One `.tif` per frame, written into a folder, deflate-compressed and losslessly.
Filenames keep the source stem (`DSC01446.ARW` → `DSC01446.tif`) so each frame
still lines up with its RAW and its sidecar.

Use this when the frames are going anywhere that cannot read ProRes:

- a raw editor such as **darktable**
- **focus stacking** and matting
- **photogrammetry / COLMAP**

Be aware of the size: 16-bit RGB is 6 bytes a pixel, so a full-frame sequence
runs to roughly 100 MB a frame before compression. The app prints an estimate
before it starts.

## Tone curve

The **S-Log3 tone curve** checkbox applies to both formats. It is on by default,
which is what you want for ProRes.

For a TIFF sequence the choice matters:

| | looks like | use when |
|---|---|---|
| **S-Log3 on** | flat, washed out | you will apply a log-to-linear LUT or curve when grading |
| **S-Log3 off** | very dark | you want maximum latitude and will set the input profile yourself |

Neither is wrong, but both need telling the editor what it is holding. The RAW is
decoded scene-linear (`gamma=(1, 1)`), so with the curve off the files really are
linear — and a linear TIFF opened as though it were sRGB looks nearly black.

**In darktable:** set the input colour profile to *linear Rec709 RGB* and use
*filmic rgb* or *sigmoid*. Then scene-linear TIFFs behave like raw files and you
keep the full highlight latitude.

## Requirements

    pip3 install rawpy numpy opencv-python tifffile
    brew install ffmpeg exiftool

`tifffile` is only needed for TIFF output, and is imported lazily — ProRes export
works without it. `ffmpeg` is only needed for ProRes.

## Run

    python3 timelapse_pro.py
