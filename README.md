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

## Hoe het werkt
1. **Scanner**: zoekt markten waar Polymarket dagelijkse liquidity rewards betaalt en
   schat jouw aandeel in die pool (`$/dag` en `$/dag per $100 kapitaal`).
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
