# Versioning

Parallax development builds use `v0.N`, where `N` is the number of commits
reachable from the current Git `HEAD`. The launcher also displays the short
commit hash, which identifies the exact committed source. It marks the build
as having uncommitted changes when the worktree is dirty.

This is a lightweight development label, not a promise of compatibility or a
stable release. When Parallax is ready for a supported release, switch to
explicit semantic version tags (`vMAJOR.MINOR.PATCH`) and keep the Git hash as
build metadata.
