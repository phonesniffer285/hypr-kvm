# hypr-kvm

An scrcpy fork to input share on Hyprland with seamless switching.

## About

I recently switched to Arch Linux with Hyprland, but couldn't find any input sharing solutions. Hyprland is strict with input logging, so I created my own by forking scrcpy to enable seamless keyboard and mouse sharing between your PC and phone.

## Features

- **Seamless Input Switching**: Move your mouse to the edge of the screen and back using `Ctrl+Right Alt` to switch between your PC and phone
- **Ethernet Support**: Dynamically detects IP and establishes connection over Ethernet (minimizes latency)
- **Audio Routing**: Routes audio from your phone to your PC
- **Hyprland Integration**: Works seamlessly with Hyprland's input system

WiFi support coming soon!

## Requirements

- **ydotool** - For sending keyboard and mouse inputs
- **scrcpy** - For screen sharing (or this fork)
- **adb** - Android Debug Bridge for device communication
- **Python 3.x** - For running the scripts

### Installation

```bash
# Install ydotool (Arch)
sudo pacman -S ydotool

# Install scrcpy and adb
sudo pacman -S scrcpy

# For other distros, use your package manager
# (apt, dnf, brew, etc.)
```

## Setup & Usage

1. **Enable USB Debugging** on your Android device (Settings > About > Build Number > tap 7 times > Developer Options > USB Debugging)
2. **Connect via Ethernet**: Set up Ethernet tethering on your phone
3. **Run the script**:
   ```bash
   python hypr-kvm.py
   ```

The script will automatically detect your phone's IP and establish the connection.

## Controls

- **Ctrl+Right Alt**: Toggle between PC and phone input
- Move your mouse to the screen edge to switch control

## Changelog

### v2
- Improved `Ctrl+Right Alt` switch robustness
- Better detection of cursor return to desktop

## License

This is a fork of [scrcpy](https://github.com/Genymobile/scrcpy).
