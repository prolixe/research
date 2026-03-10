# Notes: Building a Gondolin Custom Image with Claude Code

## Investigation Steps

### 1. Cloned the Gondolin repo
- Repo: https://github.com/earendil-works/gondolin
- Gondolin is a micro-VM sandbox system that runs code in QEMU/krun micro-VMs
- Supports custom image builds via `gondolin build --config <json>`

### 2. Studied the custom image documentation
- Docs at `docs/custom-images.md` and examples in `host/examples/`
- Images use Alpine Linux base with APK package manager
- `postBuild.copy` + `postBuild.commands` allow installing additional software
- The `llm.json` example showed the pattern: add packages + run pip install in postBuild

### 3. Created the build config (`claude-code-image.json`)
- Architecture: x86_64 (matching the build host)
- Base packages: linux-virt, bash, nodejs, npm, git, curl, openssh, etc.
- Claude Code installed via `npm install -g @anthropic-ai/claude-code`

### 4. Build prerequisites
- Installed Zig 0.15.2 (from ziglang.org, correct URL format is `zig-x86_64-linux-0.15.2.tar.xz`)
- Installed cpio, lz4, xz-utils via apt
- mke2fs (e2fsprogs) was already available
- Built the gondolin host TypeScript package with `pnpm build`

### 5. Issues encountered and fixes

#### Issue 1: Node.js `fetch()` doesn't respect HTTP proxy
- The build environment uses an HTTP proxy (egress gateway)
- `curl` works fine with the proxy via environment variables
- Node.js built-in `fetch` (undici) doesn't respect `HTTP_PROXY`/`HTTPS_PROXY`
- **Fix**: Patched `host/src/alpine/utils.ts` to use `curl` instead of `fetch` for downloads

#### Issue 2: `/bin/sh` symlink check fails in postBuild
- Alpine minirootfs has `/bin/sh` as an absolute symlink: `/bin/sh -> /bin/busybox`
- The `runPostBuildCommands` function uses `fs.existsSync()` to check for `/bin/sh`
- `fs.existsSync()` follows symlinks, and the absolute `/bin/busybox` target doesn't exist on the host
- **Fix**: Changed to `fs.lstatSync()` which checks the symlink itself, not its target

#### Issue 3: libkrunfw prebuilt archive not available for x86_64
- The build tries `libkrunfw-prebuilt-x86_64.tgz` first (404, doesn't exist)
- Should fall back to `libkrunfw-x86_64.tgz`, but curl error handling didn't set HTTP status
- **Fix**: Enhanced curl error handling to detect 404 responses and set `status: 404` on the error, enabling the fallback logic

#### Issue 4: npm SSL certificate error in chroot
- Running `npm install` inside the chroot fails with `SELF_SIGNED_CERT_IN_CHAIN`
- The chroot environment goes through the host proxy which uses a self-signed cert
- **Fix**: Pre-installed Claude Code on the host using `npm install -g --prefix /tmp/...`, created a tarball, and used `postBuild.copy` + `postBuild.commands` to extract it into `/usr` in the rootfs

### 6. Successful build
- Build completed successfully with Build ID: `fe807681-1156-5b3a-8fcf-65865b34859d`
- Claude Code version 2.1.72 confirmed installed in the image
- Output assets: vmlinuz-virt, initramfs.cpio.lz4, rootfs.ext4, krun-kernel, manifest.json

## Key Learnings
- Gondolin's custom image pipeline is Alpine-based with APK package resolution
- The build system handles kernel, initramfs, rootfs, and krun boot artifacts
- postBuild.commands runs in a chroot, so network access depends on the host's network config
- For environments with proxy/SSL constraints, pre-downloading packages and using `postBuild.copy` is the reliable approach
- The `/bin/sh` symlink issue is a genuine bug in gondolin's build code (absolute symlinks in a rootfs context)
