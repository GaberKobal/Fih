# Legal sources and confidence

These notes were attached to the data files as `_note` and `_legal_note` keys until every comment was removed from the repository (commit `861f85e`). They are kept here word for word because they record where the legal data came from and how far to trust it. Nothing in the app reads this file.

The originals are recoverable from history, for example `git show 84aa3fd:ES.json` or `git show 84aa3fd:species_oahu.json`.

## Jurisdiction packs

Five packs carry rules: ES, FR, GR, IT and PT, at the repository root as `<CC>.json`. The country, confidence, official source and date below are fields that are still inside each pack. The removed note repeated the same source address and added how much to trust the rest.

| Pack | Country | Confidence | Official source | Checked | Closed seasons researched |
|---|---|---|---|---|---|
| ES | Spain | framework only | https://www.mapa.gob.es | 2026-08-06 | no |
| FR | France | framework only | https://www.mer.gouv.fr | 2026-08-06 | no |
| GR | Greece | framework only | https://www.hcg.gr | 2026-08-06 | no |
| IT | Italy | framework only | https://www.politicheagricole.it | 2026-08-06 | no |
| PT | Portugal | framework only | https://www.dgrm.pt | 2026-08-06 | no |

The same note was on all five packs, identical except for the jurisdiction code (written `<CC>` below) and a last line, `Primary source: <address>`, which gave the pack's official source from the table above:

```
Fishing rules applied to regions with jurisdiction <CC>.

THREE TIERS OF CONFIDENCE, and the app shows which is which:
  min_size_cm from EU Reg 2019/1241 Annex IX is real law and a FLOOR -
    the national rule may be stricter and often is.
  framework rules (licence, night, scuba, distances) are widely
    reported and stable, but were NOT taken from a primary source
   except for HR. Treat <CC> as a prompt to check, not an answer.
  closed_seasons are researched for HR only. An empty list here means
    NOT RESEARCHED, not 'no closure exists'.
```

## Species files

Each species file carried a `_legal_note`. The first, for `species.json` (Croatia), is the only one that records research and a check date. The Azores note says the Portuguese pack applies nationally and names a regional rule the app cannot show. The others say that no legal pack exists for that place and name rules the app does not encode.

### `species.json`

> Closed seasons and sizes below are a CONVENIENCE, not legal advice, and
> they were checked once on 2026-07-29. Croatian rules are amended year to
> year. An empty closed_seasons list means I found no closure - not that
> none exists. Verify against the current Pravilnik o zastiti riba i drugih
> morskih organizama at ribarstvo.mps.hr before every season.
>
> Spearfishing in Croatia needs a SPORT licence (not the plain recreational
> one), age 16+, no diving apparatus.

### `species_azores.json`

> Legal rules live in legal/PT.json and apply nationally - Portugal's mainland size/season entries do not necessarily match Azorean regional measures. One important regional rule this app cannot currently show: the Regional Government of the Azores prohibits spearfishing for dusky grouper (Epinephelus marginatus, 'mero') outright, which is why it is deliberately not listed as a target species here even though it is present and commonly seen. Always check current Azorean regional rules, not just national PT ones, before diving.

### `species_bali.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for ID, so this app will correctly show 'not researched' rather than any size or season claim. Indonesian regulations vary by region, and spearfishing with scuba (as opposed to freediving) is prohibited in Bali specifically - none of this is encoded anywhere in this app yet.

### `species_capetown.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for ZA, so this app will correctly show 'not researched' rather than any size or season claim - South Africa publishes real, actively-enforced size and bag limits (e.g. galjoen and red roman both carry specific minimum sizes and daily limits) that are not encoded anywhere in this app yet.

### `species_florida_keys.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for US-FL, so this app will correctly show 'not researched' rather than any size or season claim - the Florida Fish and Wildlife Conservation Commission (FWC) publishes the real, actively-enforced rules, including no-spearfishing zones within the Florida Keys National Marine Sanctuary and species that cannot be speared anywhere in Florida (snook, tarpon, redfish and others).

### `species_indian_tropical.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists for any western Indian Ocean jurisdiction, so this app will correctly show 'not researched' rather than any size or season claim. This is a coarse ocean-basin fallback, not a country-specific pack: it cannot distinguish Egypt from Kenya from the Maldives, and spearfishing is banned outright in the Maldives specifically.

### `species_nwatlantic.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for the US East Coast states, so this app will correctly show 'not researched' rather than any size or season claim - and the rules genuinely differ state to state (Massachusetts bans spearfishing striped bass and lobster outright; other states allow striped bass but regulate size and season).

### `species_oahu.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for US-HI, so this app will correctly show 'not researched' rather than any size or season claim - Hawaii DLNR publishes the real rules, including size limits on several of the species above.

### `species_okinawa.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for JP, so this app will correctly show 'not researched' rather than any size or season claim - Okinawa prefectural rules on spearfishing gear, protected species and marine protected areas are strict, vary by location, and are not encoded anywhere in this app yet.

### `species_poorknights.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for NZ, so this app will correctly show 'not researched' rather than any size or season claim - New Zealand Fisheries publishes the real rules, and critically, the Poor Knights Islands themselves are a no-take marine reserve where all fishing including spearfishing is banned; this app's region bbox covers the wider coast, not just the reserve, and the reserve boundary is NOT masked out.

### `species_rottnest.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for AU-WA, so this app will correctly show 'not researched' rather than any size or season claim - the WA Department of Primary Industries and Regional Development (DPIRD) publishes the real rules, and Rottnest Island's own sanctuary zones (e.g. Parker Point, Salmon Bay, Little Salmon Bay) ban all fishing including spearfishing outright, which this app does not currently mask out.

### `species_socal.json`

> Legal rules live in legal/<JURISDICTION>.json and are merged in at run time. No legal pack exists yet for US-CA, so this app will correctly show 'not researched' rather than any size or season claim - California Department of Fish and Wildlife (CDFW) publishes the real rules, including species with no-take status.
