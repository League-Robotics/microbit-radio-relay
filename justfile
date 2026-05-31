set dotenv-load := true

default:
    @just --list

setup-macos:
    brew uninstall arm-none-eabi-gcc arm-none-eabi-binutils || true
    brew install --cask gcc-arm-embedded
    brew install uv

link-arm-tools:
    ln -sf /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-gcc /opt/homebrew/bin/arm-none-eabi-gcc
    ln -sf /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-g++ /opt/homebrew/bin/arm-none-eabi-g++
    ln -sf /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-ar /opt/homebrew/bin/arm-none-eabi-ar
    ln -sf /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-ranlib /opt/homebrew/bin/arm-none-eabi-ranlib
    ln -sf /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-objcopy /opt/homebrew/bin/arm-none-eabi-objcopy
    ln -sf /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-size /opt/homebrew/bin/arm-none-eabi-size

uv-sync:
    uv venv
    uv sync

build:
    uv run python3 build.py

build-clean:
    uv run python3 build.py --clean

scripts-build:
    uv run python3 scripts/build.py

deploy *args='':
    uv run python3 scripts/deploy.py {{args}}

build-deploy *args='':
    uv run python3 scripts/build_and_deploy.py {{args}}
