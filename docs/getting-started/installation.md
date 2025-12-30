# Installation

## Prerequisites

### System Requirements

- **Python**: 3.10 or higher
- **FFmpeg**: Version 6.0+ with AV1 codec support
  - For GPU: FFmpeg compiled with `--enable-nvenc`
  - For CPU: FFmpeg with `libsvtav1` support
- **ExifTool**: Perl-based metadata tool (optional but recommended)
- **Operating System**: Linux, macOS, or Windows with WSL

### Check Existing Tools

```bash
# Check Python version
python3 --version  # Should be 3.10+

# Check FFmpeg
ffmpeg -version | head -1
ffmpeg -codecs | grep av1  # Should show av1_nvenc and/or libsvtav1

# Check ExifTool (optional)
exiftool -ver
```

## Installation Methods

### Method 1: UV (Recommended)

VBC uses `uv` for fast, reliable dependency management:

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone repository
cd ~/DEV/scriptoza

# Dependencies are automatically installed when running via uv
uv run vbc/main.py --help
```

### Method 2: Manual Virtual Environment

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install rich pyyaml pyexiftool typer

# Run VBC
python vbc/main.py --help
```

## Installing FFmpeg

### Linux (Ubuntu/Debian)

```bash
# Install from official repository
sudo apt update
sudo apt install ffmpeg exiftool

# For NVENC support, you may need to build from source or use PPA
sudo add-apt-repository ppa:ubuntuhandbook1/ffmpeg7
sudo apt update
sudo apt install ffmpeg
```

### macOS

```bash
# Using Homebrew
brew install ffmpeg exiftool

# For NVENC support (requires NVIDIA GPU and drivers)
brew install ffmpeg --with-nvenc
```

### Arch Linux

```bash
sudo pacman -S ffmpeg perl-image-exiftool
```

## Installing ExifTool

ExifTool is required for deep metadata analysis and camera filtering.

### Linux
```bash
# Debian/Ubuntu
sudo apt install libimage-exiftool-perl

# Arch
sudo pacman -S perl-image-exiftool

# Fedora
sudo dnf install perl-Image-ExifTool
```

### macOS
```bash
brew install exiftool
```

### Manual Installation
```bash
# Download from official site
wget https://exiftool.org/Image-ExifTool-12.70.tar.gz
tar -xzf Image-ExifTool-12.70.tar.gz
cd Image-ExifTool-12.70
perl Makefile.PL
make
sudo make install
```

## Verify Installation

```bash
# Check all dependencies
uv run vbc/main.py --help

# You should see the help message without errors
# Test with a small video file
uv run vbc/main.py /path/to/test/video --threads 1 --cq 45
```

## GPU Support (NVIDIA)

For GPU-accelerated compression with NVENC:

1. **Install NVIDIA Drivers**: Version 470+ recommended
   ```bash
   nvidia-smi  # Check driver version
   ```

2. **Verify NVENC support**:
   ```bash
   ffmpeg -codecs | grep nvenc
   # Should show: av1_nvenc, hevc_nvenc, h264_nvenc
   ```

3. **Test GPU encoding**:
   ```bash
   uv run vbc/main.py /path/to/video --gpu --threads 2
   ```

!!! warning "Hardware Limitations"
    - NVENC session limits vary by GPU:
        - Consumer GPUs (RTX 30-series): ~5 concurrent sessions
        - RTX 40-series (e.g., 4090): 10-12 concurrent sessions
        - Professional GPUs (Quadro, A-series): Higher limits
    - 10-bit AV1 requires RTX 40-series or newer
    - VBC automatically detects "Hardware is lacking required capabilities" errors

## Next Steps

- [Quick Start Guide](quickstart.md) - Compress your first video
- [Configuration](configuration.md) - Customize VBC settings
