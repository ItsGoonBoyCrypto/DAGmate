# Chess piece artwork

The SVGs in this directory are the **chessnut** set by
[Alexis Luengas](https://github.com/LexLuengas/chessnut-pieces), obtained via
[lichess-org/lila](https://github.com/lichess-org/lila) (`public/piece/chessnut`).

**License: [Apache-2.0](https://github.com/LexLuengas/chessnut-pieces/blob/master/LICENSE.txt)**

Apache-2.0 is permissive — no copyleft obligation on DAGmate itself. Keep this
file with the assets to satisfy the attribution requirement.

## Why not cburnett

The lichess default set (**cburnett**, by Colin M.L. Burnett) was used first and
looks slightly more classical, but it is **GPLv2+ / copyleft**. Swapped out
deliberately so DAGmate's distribution terms stay unencumbered. Do not swap back
without a licensing opinion.

## Swapping sets

`renderBoard()` in `js/app.js` builds the path `assets/pieces/{w|b}{KQRBNP}.svg`.
Any set using those 12 filenames is a drop-in replacement — note that viewBox
dimensions differ between sets (cburnett is 45×45, chessnut is 800×800), which
does not matter because the pieces are sized in CSS (`.piece { width: 86% }`)
and rendered via `<img>`.

Other permissively-licensed sets in `lichess-org/lila` → `public/piece/`, with
licences listed in that repo's `COPYING.md`.
