# Polymarket market-making bot (paper trading)

Simuleert market making op Polymarket met **nepgeld** op **live** marktdata.
Er worden geen echte orders geplaatst en er is geen account of API-sleutel nodig.

## Installeren
```bash
cd polymarket_mm
pip install -r requirements.txt
```

## Gebruik
```bash
python -m mm scan                       # beste markten voor liquidity rewards
python -m mm paper --bankroll 500       # bot draaien met $500 nepgeld (Ctrl-C stopt)
python -m mm paper --hours 72           # 3 dagen laten lopen
python -m mm report                     # samenvatting van de run
python -m unittest discover -s tests    # tests
```

Belangrijke opties: `--size` (shares per kant), `--spread-frac` (afstand tot mid),
`--max-pos` (max positie per markt), `--markets` (aantal markten), `--slug` (specifieke markt).

## 24/7 op een Mac (mini)
```bash
git clone <repo-url> && cd <repo-map>
BANKROLL=1000 MARKETS=6 ./mac/bot.sh install   # eenmalig
```
De bot start dan automatisch, ook na een herstart of crash, en gaat verder waar hij was.

| Commando | Doet |
|---|---|
| `./mac/bot.sh status` | draait hij? + samenvatting (fills, P&L, beloningen, per dag) |
| `./mac/bot.sh log` | live meekijken |
| `./mac/bot.sh stop` / `start` | pauzeren / hervatten |
| `./mac/bot.sh reset` | oude resultaten archiveren en opnieuw beginnen met nieuwe markten |
| `./mac/bot.sh uninstall` | automatisch starten verwijderen |

Zet in **Systeeminstellingen** het volgende aan:
- **Energie**: "Voorkom automatische sluimerstand" en "Start automatisch op na stroomstoring"
- **Gebruikers**: automatisch inloggen

Python 3.9 of nieuwer is nodig. Heb je dat nog niet: `xcode-select --install`.

## Hoe het werkt
1. **Scanner**: zoekt markten waar Polymarket dagelijkse liquidity rewards betaalt en
   schat jouw aandeel in die pool (`$/dag` en `$/dag per $100 kapitaal`).
   Risicovolle markten worden standaard overgeslagen:
   - eindigt binnen 30 dagen (`--min-days`)
   - datumvenster in de vraag, zoals "between Nov 9 and Nov 15", "on Dec 10" of "by Nov 30"
   - sport en live-events
   - mid buiten 0,20-0,80 (`--min-mid`, `--max-mid`)
   - beweeglijk: stdev van de dagbeweging boven 4c (`--max-vol`) of een dagsprong boven 10c in de laatste maand (`--max-jump`)

   Met `--allow-risky` zet je het datumvenster- en sportfilter uit.
2. **Strategie**: zet een bod en een laat rond de middenprijs, binnen de reward-band,
   en verschuift die op basis van je positie (voorraadbeheer).
3. **Paper exchange**: je order sluit achteraan in de wachtrij aan en wordt alleen
   gevuld als er echt gehandeld wordt tegen of door jouw prijs.
4. **Risico**: maximale positie, noodstop bij koerssprongen, geen markten vlak voor de uitslag.

Logs komen in `logs/` (`fills.csv`, `equity.csv`, `state.json`).

## Let op
- Beloningen zijn een **schatting** (benadering van de Polymarket-formule, concurrentie verandert continu).
- Paper-resultaten zijn geen garantie: echte fills kunnen slechter uitvallen.
- Controleer of Polymarket in jouw land is toegestaan voordat je ooit met echt geld handelt.
