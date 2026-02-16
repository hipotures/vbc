# Installation

## Prerequisites

### System Requirements

- **Python**: 3.12 or higher
- **FFmpeg**: Version 6.0+ with AV1 codec support
  - For GPU: FFmpeg compiled with `--enable-nvenc`
  - For CPU: FFmpeg with `libsvtav1` support
- **ExifTool**: Perl-based metadata tool (required in current runtime flow)
- **nvtop**: GPU monitoring tool for sparklines (optional; NVIDIA GPUs only)
- **Operating System**: Linux, macOS, or Windows with WSL

### Check Existing Tools

```bash
# Check Python version
python3 --version  # Should be 3.12+

# Check FFmpeg
ffmpeg -version
ffmpeg -codecs | grep av1  # Should show av1_nvenc and/or libsvtav1

# Check ExifTool
exiftool -ver

# Check GPU monitoring tool (optional)
nvtop -s
```

## Installation Methods

### Method 1: UV (Recommended)

VBC uses `uv` for fast, reliable dependency management:

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone repository and enter project
git clone https://github.com/your-org/vbc.git
cd vbc

# Install dependencies from lockfile (reproducible)
uv sync --frozen
# If you intentionally updated dependencies or uv.lock:
# uv sync

# Bootstrap runtime config (required)
cp conf/vbc.yaml.example conf/vbc.yaml

# Verify CLI works
uv run vbc --help
```

### Method 2: Manual Virtual Environment

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install rich pyyaml pyexiftool typer pydantic

# Run VBC
python -m vbc.main --help
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
uv run vbc --help

# You should see the help message without errors.
# Test with a small input directory
uv run vbc /path/to/test/videos --threads 1 --quality 45
```

## GPU Support (NVIDIA)

For GPU-accelerated compression with NVENC:

1. **Install NVIDIA Drivers**: Version 470+ recommended
   ```bash
   nvidia-smi  # Check driver version
   ```

2. **Install nvtop (optional, for GPU sparklines)**:
   - Debian/Ubuntu: `sudo apt install nvtop`
   - Arch: `sudo pacman -S nvtop`
   - macOS (Homebrew): `brew install nvtop`

3. **Verify NVENC support**:
   ```bash
   ffmpeg -codecs | grep nvenc
   # Should show: av1_nvenc, hevc_nvenc, h264_nvenc
   ```

4. **Test GPU encoding**:
   ```bash
   uv run vbc /path/to/videos --gpu --threads 2
   ```

!!! warning "Hardware Limitations"
    - NVENC session limits vary by GPU:
        - Consumer GPUs (RTX 30-series): ~5 concurrent sessions
        - RTX 40-series (e.g., 4090): 10-12 concurrent sessions
        - Professional GPUs (Quadro, A-series): Higher limits
    - VBC keyboard runtime controls (`<`/`>`) clamp threads to 1-8
    - 10-bit AV1 requires RTX 40-series or newer
    - VBC automatically detects "Hardware is lacking required capabilities" errors

## Next Steps

- [Quick Start Guide](quickstart.md) - Compress your first video
- [Configuration](configuration.md) - Customize VBC settings
