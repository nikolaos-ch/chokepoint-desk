# fonts/

`inter-latin-var.woff2` (27 KB): Inter v20, the latin file Google Fonts serves for
`family=Inter:wght@400;500;600`, subset to what the page shows: ASCII, Latin-1, common
punctuation (– — ‘ ’ “ ” … • ·), ≤ ≥ ← → ↗ and the minus sign, with the weight axis cut
to 400–600. The page uses weights 400, 500 and 600 only. Any other character falls back
to the system font.

It is served from this site, not from fonts.googleapis.com, so that a slow network does
not pay for two extra connections. On Fast 3G that keeps the page under its 3 s budget.

Rebuilding it: run [subset-font](https://www.npmjs.com/package/subset-font) on the Google
file with the character list above and `variationAxes: { wght: { min: 400, max: 600 } }`.

Licence: SIL Open Font License 1.1, © The Inter Project Authors
(https://github.com/rsms/inter, https://openfontlicense.org).

Deploy: `scripts/build_site.sh` must copy this folder into `public/` (`cp -r fonts public/`).
