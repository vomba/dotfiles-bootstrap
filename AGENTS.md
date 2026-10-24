# dotfiles-bootstrap

Public portable native dotfiles engine/assets for Ubuntu 24.04 x86_64 and macOS arm64 (15+). No Nix prerequisite. See README for the source and operation contract.

## Boundaries

- Keep private identity/secrets, encrypted user inputs, work paths/wrappers, AI/OMP/Zed/MCP/skills, caches/vaults/sessions out of this repository.
- Public standalone must work without private inputs. Consumers automatically acquire immutable source by exact Git revision plus archive SHA-256; do not require a fixed secondary checkout.
- Manifests under `native/manifests/` declare packages, feature conditions, trusted executable paths and lifecycle ownership. Chezmoi owns authored files; mise/locked recipes/native providers own selected runtimes. One explicit owner per command/service. No second state engine or arbitrary script execution gateway.
- Use platform profiles `linux-x64` / `macos-arm64`; preserve independent feature flags. Default tools do not silently bypass missing strict mise shims.
- Preserve foreign files/replacements, shared native packages/services, credentials and application data. Never infer ownership solely from a filename or package-store path.

## Operations

Use `./bootstrap --help` and actual action-specific options. Bootstrap, provisioning, configuration apply, service transitions and publication are separate explicit operations. Apply never downloads packages, decrypts credentials or starts services. Shell startup/directory entry never installs or updates. Mutations require --yes and actual authorization; a flag is not consent obtained by reading instructions.

Native-host provisioning uses pinned reviewed official Homebrew installer source only after explicit authorization. Apple CLT/Accessibility/browser/license consent cannot be bypassed. Shared Homebrew/APT/Docker/Snap resources are retained at retirement, never broad cleanup/prune.

Source updates are repository-owned managed public-only worktrees from a consumer's immutable pin: source prepare/check never publishes or changes live selection; source publish is separately authorized signed commit + normal push + fresh readback + consumer pin update. Never mutate the immutable cache or force-reset foreign work. No built-binary publication is implied.

## Verification and review

Use the native `verify --checks` entry and `native/verification/verify.py` contracts. Explicit `bootstrap --verification-tools --yes` acquires internal pinned checker dependencies; do not substitute Nix tools. Public CI executes native Linux/macOS checks with public-only inputs. A platform render does not prove that platform ran; package tests do not prove physical GPU/login/audio/consent or provider authentication.

Follow existing stdlib unittest/shell suites with disposable HOME/synthetic state and behavioral/security/ownership assertions. Do not weaken meaningful consumer coverage or pin incidental implementation wording. Workers skip tests/builds/lint/formatters unless explicitly assigned; integration owner runs union checks after all changes land. Report observed checks/skips/limits honestly. Code/security review is not authorization to activate, access credentials, commit or publish.

Use placeholder consumer/repository identities in public examples. Preserve source/license attribution. Never initialize Codegraph without authorization; when no public index exists, use targeted source reads.
