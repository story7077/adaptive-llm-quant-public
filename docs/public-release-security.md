# Public release security

This repository is released from a sanitized working tree with a new Git root. Private
repository history is never merged, rebased, grafted, or force-pushed into this repository.
The release check fails closed: any unreadable file, unreadable Git object, uncertain binary,
unmarked root, or detected private material blocks publication.

## Required release gate

Run the scanner from the repository root before every public push:

```shell
python scripts/public_release_scan.py \
  --root . \
  --expected-repository story7077/adaptive-llm-quant-public
```

The default invocation checks both the current working tree and every blob reachable through
every local Git ref. `--worktree-only` exists solely for diagnostics before Git initialization;
it is not an acceptable release result. CI always performs the complete history scan.

The scanner emits only a rule identifier, safe file path, and optional line number. It deliberately
does not print the matching value. A filename that itself resembles private material is replaced
with a short one-way path fingerprint. A failed CI log therefore does not reproduce a credential
or personal value.

## Clean-root proof

`public-release-root.json` must be included in the first commit. The scanner requires:

- exactly one reachable root commit;
- the marker to be present and valid in that root commit;
- `private_history_imported` to be false;
- the marker repository name to match the public GitHub repository.

Adding the marker in a later commit does not pass. If sensitive content enters any commit,
deleting it in a subsequent commit also does not pass because historical blobs are scanned.
Rebuild a fresh sanitized root instead of rewriting or force-pushing an unsafe public history.

## Material blocked by policy

The gate rejects:

- known credential and private-key token shapes;
- non-placeholder values assigned to credential fields;
- non-example email addresses and non-synthetic account identifiers;
- absolute workstation and user-home paths;
- local environment, account, database, cache, log, browser-profile, cookie, and session files;
- raw market, news, browser, research-run, and model-response artifacts;
- Git LFS pointers or LFS filter configuration;
- binary, oversized, non-UTF-8, symlink, and submodule entries;
- a paper-account fixture that does not satisfy the synthetic fixture contract.

All examples use reserved example identities and synthetic integer quantities. Raw licensed
content belongs in ignored local object storage. Public evidence records contain only permitted
metadata, hashes, short lawful excerpts, and licensing notes.

## Release procedure

1. Export an allowlisted working tree without copying the private `.git` directory.
2. Replace account material with `config/paper-account.example.yaml`.
3. Remove generated caches and all local runtime output.
4. Run the worktree diagnostic and resolve every finding.
5. Initialize a new repository and include the root marker in the first commit.
6. Run the complete scanner and its regression tests.
7. Push a normal branch without force-push and wait for the GitHub release gate.
8. Create the public pull request only after the gate passes.

Scanner exceptions are not accepted through command-line allowlists. A deliberate policy change
requires a reviewed code and test change to the protected workflow.

## Recovery

If a secret or personal value is detected before push, revoke it if applicable, remove the source
file, and create a new clean root if it was committed locally. If exposure reaches GitHub, revoke
the credential immediately, stop publication, preserve an incident record outside this repository,
and rebuild the public repository from a newly sanitized snapshot. Deleting only the latest file is
not sufficient.
