module smartchat/bridge-wa

go 1.22

// Dependencies are intentionally NOT pinned here. The Docker build runs
// `go mod tidy` (see infra/bridge-wa.Dockerfile) which resolves the whatsmeow
// stack + modernc pure-Go sqlite + protobuf at their latest compatible
// versions and generates go.sum at build time. whatsmeow has no stable semver
// and ships breaking API changes often, so we target the CURRENT API and let
// tidy fetch it fresh. The whatsmeow call sites are isolated in device.go /
// manager.go and enumerated in README.md so a post-build signature drift is a
// one-line fix confined to those files.
