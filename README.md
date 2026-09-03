# TimelapsePro

Timelapse post-processing for PiSlider + Sony camera sequences.

Reads Sony ARW RAW files together with the XMP sidecars PiSlider writes during
a shoot, and applies the corrections that need rig metadata to do properly:

- **Exposure correction** from `crs:Exposure2012`
- **White balance** — the camera-commanded WB embedded in the RAW
- **Tilt correction** from `ps:Rig_Tilt_Deg`, so a levelled horizon survives a
  slider move that was not perfectly level
- **S-Log3 tone encoding**
- **Export to Apple ProRes 4444** via ffmpeg

Because the rig records its own tilt per frame, the correction is driven by
measured geometry rather than estimated from image content.

## Requirements

    pip3 install rawpy numpy opencv-python
    brew install ffmpeg exiftool

## Run

    python3 timelapse_pro.py
