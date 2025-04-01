## Session Continuous Integration docker build images

This repo manages the Session project CI docker images used to build and test various Session
packages.

The repository is publicly available (read-only) at https://registry.session.codes.

If you aren't part of the Session team, you'll likely need to set up your own registry and change
reg.session.codes:80 (which resolves to an internal VPN IP address usable by for machines authorized
to push to the registry) to your own domain name to do anything useful with this.
