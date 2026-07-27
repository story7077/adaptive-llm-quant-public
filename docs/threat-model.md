# Threat model

## Security objectives

The public system must preserve private account confidentiality, credential confidentiality,
research integrity, point-in-time reproducibility, append-only evidence, and the prohibition on
real broker routing. Public code and fixtures must be independently usable without revealing the
operator's machine, browser state, accounts, or licensed raw content.

## Trust boundaries

The operational trading plane, research plane, browser scout, isolated commander, candidate
builder, OOS lockbox, public Git repository, and external data providers are separate trust
domains. Structured, hashed contracts cross these boundaries. Credentials, browser sessions,
raw source objects, private account state, hidden model reasoning, and prior ephemeral run
directories do not.

The public repository is untrusted output until the release scanner has checked its working tree,
complete reachable history, root provenance, fixture contract, and file types.

## Threats and controls

| Threat | Primary controls |
|---|---|
| Credential copied from local configuration | Ignore rules, token and assignment scans, no secret-bearing fixtures, redacted findings |
| Private Git history imported into public | New sanitized root, root marker in the first commit, full-history blob scan |
| Real account state reconstructed from fixtures | Exact synthetic account contract, non-synthetic identifier detection, integer example quantities |
| Local identity or topology disclosed | Absolute home/workstation path and non-example email detection |
| Browser session or raw licensed content committed | Browser artifact and raw payload path bans, local ignored object storage |
| Secret hidden in deleted history | Scan every blob reachable from every ref |
| Payload hidden as binary, LFS, symlink, or submodule | Binary and UTF-8 checks, LFS rejection, symlink/submodule rejection |
| Scanner leaks the value it found | Findings contain only rule, path, and line |
| CI bypass through shallow checkout | Full-depth checkout and clean-root verification |
| Candidate changes protected trading code | Separate candidate path allowlist and validation pipeline |
| AI mutates Champion or routes live orders | Versioned Challengers, manual promotion, broker routing hard-disabled |
| Research result is contaminated by prior runs | Fresh conversations, fresh processes, context manifests, directory jail |
| Future-data or OOS leakage inflates results | Point-in-time contracts, locked OOS service, falsification and replay gates |

## Attacker capabilities considered

The model, an external source, a malformed research artifact, or a contributor may attempt to
place private data in source, configuration, documentation, generated output, a deleted commit,
or a disguised file. A contributor may also attempt to import an unrelated Git root or make a
protected workflow less strict.

The scanner does not assume file extensions are truthful and does not follow symbolic links.
Protected branch review must cover changes to the scanner, root marker, ignore rules, and release
workflow.

## Residual risks

Pattern-based scanning cannot prove that arbitrary prose or ordinary-looking numbers are
non-sensitive. Human review remains required for new fixtures, lawful excerpts, screenshots,
binary assets, and model-generated prose. Public paper results do not establish profitability or
live execution quality. Compromise of the local browser or host is outside the repository scanner's
control and requires operating-system isolation and credential rotation.

Automatic promotion and real broker routing remain unavailable. A scanner pass is a publication
precondition, not authorization to trade or a substitute for human review.
