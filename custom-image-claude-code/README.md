# Gondolin Custom Image with Claude Code

## Overview

This folder contains the configuration and artifacts for building a [Gondolin](https://github.com/earendil-works/gondolin) custom micro-VM image that includes [Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code) pre-installed.

Gondolin is a local Linux micro-VM sandbox system that runs untrusted code inside fast QEMU/krun VMs with controlled network and filesystem access. By baking Claude Code into a custom image, the CLI tool is immediately available when a VM boots — no runtime installation needed.

## Files

| File | Description |
|------|-------------|
| `claude-code-image.json` | Gondolin build configuration for the custom image |
| `manifest.json` | Build output manifest with checksums and build metadata |
| `gondolin-patches.diff` | Patches applied to gondolin source to fix build issues |
| `notes.md` | Detailed investigation notes and troubleshooting log |

## Build Configuration

The image is based on Alpine Linux 3.23.0 (x86_64) with:

- **Base packages**: linux-virt, bash, ca-certificates, curl, git, nodejs, npm, openssh
- **Claude Code**: v2.1.72, installed globally via npm into `/usr/lib/node_modules/@anthropic-ai/claude-code`
- **Rootfs size**: 2048 MB

## How to Build

### Prerequisites

- Node.js >= 22
- Zig 0.15.2
- cpio, lz4, e2fsprogs (mke2fs)
- pnpm

### Steps

1. Clone gondolin and install dependencies:
   ```bash
   git clone https://github.com/earendil-works/gondolin.git
   cd gondolin && pnpm install && cd host && pnpm build && cd ..
   ```

2. Pre-download Claude Code (if behind a proxy/firewall):
   ```bash
   mkdir -p /tmp/claude-code-install
   npm install -g @anthropic-ai/claude-code --prefix /tmp/claude-code-install
   cd /tmp/claude-code-install && tar czf /path/to/claude-code-files.tar.gz lib bin
   ```

3. Build the image:
   ```bash
   gondolin build --config claude-code-image.json --output ./claude-code-assets
   ```

### Using the Image

```bash
# Via environment variable
GONDOLIN_GUEST_DIR=./claude-code-assets gondolin bash

# Via build ID (after first build, cached in ~/.cache/gondolin)
gondolin bash --image fe807681-1156-5b3a-8fcf-65865b34859d

# Verify Claude Code is available inside the VM
claude --version
```

### Programmatic Usage (TypeScript SDK)

```typescript
import { VM } from "@earendil-works/gondolin";

const vm = await VM.create({
  sandbox: { imagePath: "./claude-code-assets" },
});

const result = await vm.exec("claude --version");
console.log(result.stdout); // "2.1.72 (Claude Code)"

await vm.close();
```

## Build Issues & Patches

Three issues were encountered and fixed during the build (see `gondolin-patches.diff`):

1. **Node.js fetch vs proxy**: Replaced `fetch()` with `curl` in the download helper for proxy compatibility
2. **Absolute symlink check**: Fixed `/bin/sh` existence check to use `lstatSync` instead of `existsSync` (handles absolute symlinks in rootfs)
3. **404 detection for curl fallback**: Enhanced error handling to detect HTTP 404 from curl, enabling proper fallback from prebuilt to shared libkrunfw archives

## Build Output

The successful build produces:
- `vmlinuz-virt` — Linux kernel (12 MB)
- `initramfs.cpio.lz4` — Compressed initramfs (6 MB)
- `rootfs.ext4` — Root filesystem with Claude Code (2 GB)
- `krun-kernel` — libkrunfw-compatible kernel (19 MB)
- `manifest.json` — Build metadata and SHA-256 checksums
