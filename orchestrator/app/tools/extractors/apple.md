# apple.com — browser-agent hints

These hints are injected into the configurator's system prompt when the
deterministic `apple.py` extractor returns thin results (e.g. on a
`/shop/buy-...` PDP where prices are gated behind variant pickers).

## URL shapes

- **Refurbished PDPs** — `/shop/product/<sku>/...` (e.g. `ftqu3ll/a`).
  Each variant has its own URL; the dropdowns navigate via form POST.
  Static HTML usually has full price and JSON-LD; deterministic extractor
  handles these without you.
- **Standard PDPs** — `/shop/buy-iphone/...`, `/shop/buy-mac/...`.
  Prices appear only after the user picks storage, color, carrier, and
  trade-in answers. This is where you're usually needed.

## Selectors that work

- Product title: `h1[data-autom="productTitle"]`
- Current price: `.rf-pdp-currentprice` (visible "Now $759.00")
- Price (configured page): `[data-autom="full-price"]`,
  `.as-pricepoint-fullprice`, or `.rc-prices-fullprice`
- "Add to bag" button: `button[data-autom="add-to-cart"]`
  (its `disabled` attribute is the most reliable in-stock signal — it
  disables when an unavailable variant is selected)
- Out-of-stock signal in form: `data-eVar20` substring "Out of stock"
- JSON-LD: `script[type="application/ld+json"]` with `@type:"Product"`
  carries price, condition, shipping, and return policy on refurb PDPs

## Variant pickers

Storage and color live in `<select>` elements with stable names:

- `select[name="dimensionCapacity"]` — options like `128gb`, `256gb`,
  `512gb`, `1tb`. Match `spec.must_haves` storage strings against the
  option's visible label (`256GB`) or value (`256gb`).
- `select[name="dimensionColor"]` — options like `naturaltitanium`,
  `bluetitanium`. Match against `spec.must_haves` color strings on the
  visible label, not the slugged value.
- On `/shop/buy-iphone/...`, pickers are radio-button cards instead of
  `<select>`. Each card has `data-autom="dimensionCapacity-{value}"` or
  similar. Click the matching card, then wait for the price block to
  re-render (the page does a partial fetch — wait on
  `[data-autom="full-price"]` text mutation, not navigation).

## Carrier / unlocked

Title containing "(Unlocked)" is the explicit signal. For carrier-locked
PDPs, look for `select[name="carrierModel"]` — "Connect to any carrier
later" is the unlocked option.

## Pitfalls

- The "Was $999.00" struck-through price (`.rf-pdp-previousprice`) is
  NOT the current price. Always read `.rf-pdp-currentprice`.
- Apple uses U+00A0 (NBSP) inside product names — "iPhone 15\u00a0Pro".
  Preserve it; downstream matching against canonical product names is
  fuzzy enough.
- Image URLs from `store.storeimages.cdn-apple.com` carry signed query
  params; keep the full URL, don't strip the query string.
- No login wall, no captcha, no geo gating for US PDPs. If you hit a
  region picker (`/choose-country-region`), the page is mis-routed —
  retry with `https://www.apple.com/shop/...` rather than chasing the
  picker.
- Apple's site lazily revises prices via JS on `/shop/buy-...`. Always
  wait for a non-empty `[data-autom="full-price"]` text node before
  reading; the initial paint shows "From $X" placeholders.
