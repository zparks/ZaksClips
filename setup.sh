#!/bin/bash
set -e

echo ""
echo "Chess Clip Automator — Setup"
echo "============================"
echo ""

# Homebrew
if ! command -v brew &> /dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi

# ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "Installing ffmpeg..."
    brew install ffmpeg
else
    echo "✓ ffmpeg already installed"
fi

# Python packages
echo "Installing Python packages..."
pip3 install --quiet mlx-whisper playwright requests python-dotenv

# Playwright browser (Chromium only — smaller download)
echo "Installing Playwright browser..."
python3 -m playwright install chromium

# .env setup
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ACTION REQUIRED: Create Twitch API credentials"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  1. Go to: https://dev.twitch.tv/console"
    echo "  2. Click 'Register Your Application'"
    echo "  3. Name: anything (e.g. 'clip-automator')"
    echo "  4. OAuth Redirect URL: http://localhost"
    echo "  5. Category: Other"
    echo "  6. Copy Client ID and Client Secret"
    echo "  7. Paste them into: clipper/.env"
    echo ""
else
    echo "✓ .env already exists"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete!"
echo "  Fill in .env then run: python3 clipper.py"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
