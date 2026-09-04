# Contributing

Thanks for your interest! 🐧

This repository is a **personal artifact** maintained by the author for
their own use. **External contributions (pull requests, issues,
discussions) are not accepted** at this time. Issues, the wiki,
projects, and discussions are intentionally disabled on the repository
settings; pull requests opened by non-owners are automatically closed
by [a GitHub Actions workflow](.github/workflows/close-prs.yml).

The project is MIT-licensed (see [LICENSE](LICENSE)) — you're encouraged
to **fork it freely** and adapt it for your own purposes. If you spot a
bug or have a feature idea, the most useful thing you can do is fix it
in your own fork.

## Why no contributions?

The codebase encodes a lot of opinions about specific provider/distro
trade-offs, surfaces many "battle scars" that document why things are
the way they are, and the README is dense with specific reasoning. Drive-by
PRs risk eroding that internal consistency. Forking is the right
pattern: take what's useful, change what you want, move on.

## What if I really want to upstream a fix?

The repository owner may revisit this policy in the future. If/when
that happens, this file and the auto-close workflow will be updated.

## Security issues

For any **security-sensitive** issue (e.g., the generated VM definitions
leaking secrets, the cloud-init templates accidentally enabling unsafe
defaults, etc.), please contact the repository owner directly via
GitHub rather than opening a public issue or PR.
