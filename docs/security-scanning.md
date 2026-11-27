# CI security scanning

Workflow: `.github/workflows/security-scan.yml` (push and PR to `main`, plus weekly). Actions are pinned to commit SHAs
(gitleaks-action v3.0.0, trivy-action v0.36.0); update them deliberately and re-check the SHA against the release.
`actions/checkout` is still pinned by tag like the rest of CI.

| Job | Tool | Fails the build when |
|---|---|---|
| Secret scan | gitleaks over **full git history**, config `.gitleaks.toml`, findings redacted, no artifact upload | any non-allowlisted match |
| Dependency and config scan | Trivy `fs` (vuln, misconfig) | HIGH or CRITICAL with a fix available |
| Image scan | Trivy `image` of the built Dockerfile | HIGH or CRITICAL with a fix available |

Thresholds: HIGH and CRITICAL only, `ignore-unfixed` so the gate stays actionable. Unfixed and MEDIUM/LOW issues are not
reported by CI; review them with `trivy image --severity MEDIUM,LOW nexus-ai:scan` occasionally. Personal-account repos need
no gitleaks licence; an organisation must add `GITLEAKS_LICENSE` as a repository secret.

## Findings when this was added (2026-11-27)

- **Secrets (history, 89 commits):** 37 matches, all in `tests/` (fake keys, including one AWS-shaped test string) or an
  eval cache content hash (`evals/recovery/`). No real credential found. `.gitleaks.toml` allowlists those paths; it
  therefore will not catch a real secret committed under `tests/` or `evals/cache|recovery`.
- **Image:** 3 fixable HIGH findings: `libzstd1` CVE-2026-124851 (Debian), and `jaraco.context` CVE-2026-23949 and `wheel`
  CVE-2026-24049 (vendored inside setuptools). Remediated in the Dockerfile: `apt-get upgrade`, and `setuptools` and `wheel`
  are uninstalled from the runtime image. Re-scan: 0 HIGH/CRITICAL.
- **Filesystem:** `requirements.txt` and `Dockerfile`: 0 findings.

## If CI fails

- **gitleaks:** find the commit with the redacted report in the job log. If it is a real secret: revoke or rotate it at the
  provider first (removing it from git does not make it safe), then remove it from history only if needed. If it is a test
  value, use obviously fake data or add a narrow `[allowlist]` entry. Nothing is rotated or deleted automatically.
- **Trivy (OS package):** rebuild so `apt-get upgrade` picks up the fix, or move to a newer base image.
- **Trivy (Python package):** bump the pin in `requirements.in`, regenerate `requirements.txt` (command in its header), run tests.
- A finding with no fix can be accepted with a dated, reasoned entry in a `.trivyignore` file reviewed in a PR.

## Limitations

The base image tag is not pinned by digest. Scans see only known CVEs, not malicious or zero-day issues. Not run on GitHub
before this PR; local runs used the official `ghcr.io/gitleaks/gitleaks` and `aquasec/trivy` images.
