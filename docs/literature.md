# Literature

Papers that bear on what this repo actually does, gathered October 2026.

**On provenance:** no citation exists anywhere in this repo for the original
design, and the member who built it has not identified one. So this is *not* a
record of what the project was built from — it is a list of published work that
matches what was built, found afterwards. If the origin paper turns up, add it
at the top and say so.

Working papers are marked **(WP)**: verified to exist, not peer-reviewed.

---

## What an RKHS is, and whether kernels work on market data

| Paper | Covers | Use for |
|---|---|---|
| [Filipović, Pelger & Ye (2025), *Management Science*](https://doi.org/10.1287/mnsc.2023.01401) | Kernel ridge regression fitted to the cross-section of US Treasuries, dominating every parametric and nonparametric benchmark out of sample | The strongest "these methods work" citation, and it is cross-sectional |
| [Gu, Kelly & Xiu (2020), *RFS*](https://academic.oup.com/rfs/article/33/5/2223/5758276) | Machine learning across ~30,000 US stocks; one model pooled over the whole panel rather than one per stock | The pooling argument behind running one model across many markets |
| [Kozak, *Kernel Trick for the Cross-Section*](https://doi.org/10.2139/ssrn.3307895) **(WP)** | The kernel trick used to build a pricing model from nonlinear functions of characteristics | Cross-sectional kernel precedent |
| [Gretton et al. (2012), *JMLR* 13](https://www.jmlr.org/papers/v13/gretton12a.html) | The kernel two-sample test (MMD) | How kernels compare distributions rather than points |
| [Rahimi & Recht (2007), NIPS](https://proceedings.neurips.cc/paper/2007/hash/013a006f03dbc5392effeb8f18fda755-Abstract.html) | Random Fourier features — the approximation this pipeline uses | Why results move with the random seed |
| [Rudi & Rosasco (2017), NeurIPS](https://proceedings.neurips.cc/paper/2017/hash/61b1fb3f59e28c67f3925f3c79be81a1-Abstract.html) | How many random features are needed for a given error rate | Sizing the feature count honestly |

## Closest published match to this architecture

| Paper | Covers | Use for |
|---|---|---|
| [Fletcher, Hussain & Shawe-Taylor (2010), PMLR 11](https://proceedings.mlr.press/v11/fletcher10a.html) | Multiple kernels built from order-book data, combined by a learned weighting, one instrument, intraday | The nearest match to our design, and freely readable |
| [Fletcher & Shawe-Taylor (2013), *Computational Economics*](https://link.springer.com/article/10.1007/s10614-012-9317-z) | Same design with microstructure-model kernels; reports ~55% accuracy on a three-class direction call at a 200-second horizon | The published ceiling for this family of models |
| [Kercheval & Zhang (2015), *Quantitative Finance*](https://www.tandfonline.com/doi/abs/10.1080/14697688.2015.1032546) | Kernel SVMs on limit-order-book features, fitted per stock | A simpler comparison point |

## Why our own results look the way they do

| Paper | Covers | Use for |
|---|---|---|
| [Nagel (2025), NBER WP 34104](https://www.nber.org/papers/w34104) **(WP)** | With far more features than observations, these models collapse into a similarity-weighted average of recent returns; a control that strips predictive content while preserving volatility paths leaves performance nearly unchanged | **Our shuffled-target finding, in print.** The most directly relevant paper to `POSTMORTEM.md` |
| [Kelly, Malamud & Zhou (2024), *Journal of Finance*](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13298) | The single-time-series kernel result that Nagel critiques | Read as a pair with Nagel |
| [Buncic (2025), SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5239006) **(WP)** | The complexity-performance relation disappears once forecasts are aggregated across random draws | Why a single-seed result from this pipeline means nothing |
| [Fallahgoul (2025), arXiv:2506.03780](https://arxiv.org/abs/2506.03780) **(WP)** | Information-theoretic bound: roughly 31 years of monthly data needed to learn in the high-dimensional regime | Our 18 months, in context |
| [Shen (2026), arXiv:2607.20168](https://arxiv.org/abs/2607.20168) **(WP)** | Kernel-swap control across 170 walk-forward windows on ~38,000 observations: no kernel beat equal-budget linear models (ΔIC = +0.005, p = 0.42) | Adverse precedent for "the multi-kernel structure is the edge" |

## The order-flow signals, and the critiques of them

| Paper | Covers | Use for |
|---|---|---|
| [Easley, López de Prado & O'Hara (2012), *RFS*](https://academic.oup.com/rfs/article-abstract/25/5/1457/1569929) | VPIN, as originally proposed | Where our order-flow measure comes from |
| [Andersen & Bondarenko (2015), *Review of Finance*](https://ideas.repec.org/a/oup/revfin/v19y2015i1p1-54..html) | Rebuilt with near-perfect trade classification, VPIN's forecasting power reverses; the signal came from classification error | **Why our order-flow kernel gets a weight of zero.** Also: our VPIN uses a tick-rule proxy while the trade files carry the real aggressor side |
| [Andersen & Bondarenko (2014), *J. Financial Markets*](https://ideas.repec.org/a/eee/finmar/v17y2014icp53-64.html) | VPIN is a poor volatility predictor; its apparent power is a mechanical relation with trading intensity | The control regression to run: add contemporaneous volume and realised volatility |
| [Easley, López de Prado & O'Hara (2014), rejoinder](https://www.sciencedirect.com/science/article/abs/pii/S1386418113000293) | The authors' reply | The dispute is contested, not settled |
| [Ahern (2018), NBER WP 24297](https://www.nber.org/papers/w24297) **(WP)** | Against prosecuted insider trades as ground truth, Kyle's Lambda does not reliably detect informed trading | Limits of our price-impact measure |
| [Tóth et al. (2011), *Physical Review X*](https://journals.aps.org/prx/abstract/10.1103/PhysRevX.1.021006) | Price impact is square-root, not linear | Why a fitted linear lambda is not a stable market property |
| [Lallouache & Challet (2014), arXiv:1406.3967](https://arxiv.org/abs/1406.3967) **(WP)** | Most Hawkes fits to financial data are not statistically significant; fitted timescales are short | Limits of our trade-clustering measure |
| [Kolm, Turiel & Westray (2023), *Mathematical Finance*](https://onlinelibrary.wiley.com/doi/10.1111/mafi.12413) | The effective horizon of order-book forecasts is roughly two average price changes | **The horizon mismatch.** We evaluate daily; the published decay is seconds |
| [Briola, Bartolucci & Aste (2024), arXiv:2403.09267](https://arxiv.org/abs/2403.09267) **(WP)** | High forecasting accuracy does not imply an actionable signal | Why accuracy is the wrong metric |

## Volatility, spillovers and position sizing

| Paper | Covers | Use for |
|---|---|---|
| [Moskowitz, Ooi & Pedersen (2012), *JFE*](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf) | Trend following across 58 futures, with positions scaled by inverse ex-ante volatility to a constant target | **The real source for both our direction rule and our inverse-volatility sizing.** Neither came from a paper originally; this is the citation that fits |
| [Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert (2018), *JPM*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3175538) | The Sharpe benefit of volatility targeting comes from risk assets; for commodities, bonds and FX it is negligible. Tail reduction is what survives | Why `VOL_SIZING=1` must be presented as risk control, not as alpha |
| [Moreira & Muir (2017), *Journal of Finance*](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12513) | Volatility-managed portfolios — the positive result | Context for the sizing idea |
| [Cederburg, O'Doherty, Wang & Yan (2020), *JFE*](https://www.sciencedirect.com/science/article/abs/pii/S0304405X2030132X) | Failed replication: 53 positive against 50 negative Sharpe differences across 103 strategies, significantly worse than their own null | The counter-evidence |
| [Liu, Tang & Zhou (2019), *JPM*](https://jpm.pm-research.com/content/early/2019/09/17/jpm.2019.1.107.abstract) | A volatility-timing result that evaporated once a full-sample scaling constant was removed; corrected drawdowns of 68–93% | **Bug 5 of this project, in print.** Prior art for the postmortem |
| [Barroso & Detzel (2021), *JFE*](https://www.sciencedirect.com/science/article/abs/pii/S0304405X21000775) | The gains do not survive transaction costs | Cost sensitivity |
| [DeMiguel, Martín-Utrera & Uppal (2024), *Journal of Finance*](https://onlinelibrary.wiley.com/doi/full/10.1111/jofi.13395) | Single-factor volatility management fails out of sample net of costs; only a jointly optimised multifactor version survives | The configuration closest to ours is the one that fails |
| [Diebold & Yilmaz (2012), *IJF*](https://www.sciencedirect.com/science/article/abs/pii/S016920701100032X) | Volatility spillovers across stock, bond, FX and commodity markets — limited outside the 2008 crisis | The ripple literature, with its caveat. Note their 2009 paper finds return and volatility spillover indices of *similar* magnitude (35.5% vs 39.5%) — the difference is in dynamics, not size |
| [Bollerslev, Hood, Huss & Pedersen (2018), *RFS*](https://academic.oup.com/rfs/article/31/7/2729/5001472) | Volatility dynamics are similar enough across 50+ instruments that pooled panel estimation improves out-of-sample risk forecasts | The best-supported cross-asset target available to us |
| [Zhang, Pu, Cucuringu & Dong (2025), *IJF*](https://ideas.repec.org/a/eee/intfor/v41y2025i1p377-397.html) | Spillover information from neighbouring markets alone gives no clear forecast advantage | Caution on the ripple thesis |
| [Paye (2012), *JFE*](https://www.sciencedirect.com/science/article/abs/pii/S0304405X12001316) | Macro variables Granger-cause volatility in sample but rarely beat univariate benchmarks out of sample | Same caution, older |
| [Corsi (2009), *J. Financial Econometrics*](https://academic.oup.com/jfec/article-abstract/7/2/174/856522) | The HAR model for realised volatility | The benchmark any volatility forecast must beat |

## Breadth versus focus

| Paper | Covers | Use for |
|---|---|---|
| [Ye (2008), *FAJ*](https://www.tandfonline.com/doi/abs/10.2469/faj.v64.n4.5) | IR = IC ÷ √(1/N + σ_IC²), unifying Grinold's law with strategy risk | The formula behind "breadth multiplies skill, it does not create it" |
| [Grinold (1989), *JPM*](https://www.pm-research.com/content/iijpormgmt/15/3/30) | The Fundamental Law: IR = IC × √N, for *independent* bets | The original claim |
| [Clarke, de Silva & Thorley (2002), *FAJ*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=290322) | The transfer coefficient; in practice 0.3–0.8, so halve the theoretical ratio | What constraints cost |
| [Ding & Martin (2017), *J. Empirical Finance*](https://www.sciencedirect.com/science/article/pii/S0927539817300543) | Variation in skill over time matters more than breadth for a realistic universe | Limits of adding markets |
| [Michaud, Esch & Michaud (2020), *J. Investing*](https://doi.org/10.3905/joi.2020.1.133) | With estimation error in skill, the square-root curve flattens: "beyond some point, increasing universe size is likely to be self-defeating" | Why eight good contracts beat ten mediocre ones |
| [Ilmanen & Kizer (2012), *JPM*](https://www.aqr.com/-/media/AQR/Documents/Journal-Articles/JPM-The-Death-of-Diversification-Greatly-Exaggerated.pdf) | Five components at −0.02 average correlation lift Sharpe from 0.70 to 1.44; five at +0.38 lift it from 0.40 to 0.48 | **Why the 0.5 correlation bar matters more than the contract count** |
| [Asness, Moskowitz & Pedersen (2013), *Journal of Finance*](https://onlinelibrary.wiley.com/doi/10.1111/jofi.12021) | Value and momentum correlate more strongly *across* asset classes than the asset classes do with each other | Why one signal on eight markets is not eight bets |
| [Hurst, Ooi & Pedersen (2017), *JPM*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2993026) | A century of trend following across ~67 markets | The pro-breadth evidence |
| [Bhardwaj, Gorton & Rouwenhorst (2014), *RFS*](https://academic.oup.com/rfs/article-abstract/27/11/3099/1597777) | CTA net-of-fee returns insignificantly different from zero, 1994–2012 | The base rate this project is up against |
| [Van Nieuwerburgh & Veldkamp (2010), *RES*](https://www.nber.org/system/files/working_papers/w13904/w13904.pdf) | With finite research capacity, concentration is optimal because learning has increasing returns | The case for focus, formally |
| [Daskalaki, Kostakis & Skiadopoulos (2014), *JBF*](https://www.sciencedirect.com/science/article/abs/pii/S037842661300455X) | Commodity futures are heterogeneous and not priced by equity-style factors | Caution against a single pooled cross-asset model |
| [Pesaran, Pick & Timmermann, arXiv:2404.11198](https://arxiv.org/abs/2404.11198) **(WP)** | Pooled estimation beats per-unit on average but reverses for unusual units; empirical Bayes shrinkage beats both | The resolution: shared features, per-market coefficients shrunk toward a pooled prior |
| [DeMiguel, Garlappi & Uppal (2009), *RFS*](https://academic.oup.com/rfs/article-abstract/22/5/1915/1592901) | No optimising model beat 1/N across seven datasets; estimation error swamps the gain | Why we should not over-engineer allocation |

## Judging a result

| Paper | Covers | Use for |
|---|---|---|
| [Bailey & López de Prado (2014), Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) | Adjusts an observed Sharpe for the number of configurations tried, their variance, and non-normality | The headline number we should report instead of raw Sharpe |
| [Bailey & López de Prado (2012), Sharpe Ratio Efficient Frontier](https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf) | Probabilistic Sharpe Ratio and Minimum Track Record Length | How long a record must be before a Sharpe means anything |
| [Bailey, Borwein, López de Prado & Zhu (2014), *Notices of the AMS*](https://www.ams.org/notices/201405/rnoti-p458.pdf) | How few trials it takes to manufacture a high in-sample Sharpe; minimum backtest length | Context for the 25-run tuning history |
| [Harvey & Liu (2015), *JPM*](https://papers.ssrn.com/abstract=2345489) | Haircutting a Sharpe for multiple testing, with code | Correcting a reported number |
| [Harvey, Liu & Zhu (2016), *RFS*](https://academic.oup.com/rfs/article/29/1/5/1843824) | A newly discovered factor should clear t > 3.0, not 2.0 | Why Sharpe 1.56 over 1.9 years (t ≈ 2.1) would not have survived regardless |
| [McLean & Pontiff (2016), *Journal of Finance*](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365) | Published predictors return 26% less out of sample and 58% less after publication | What to expect from any signal found in the literature |
| [Hou, Xue & Zhang (2020), *RFS*](https://academic.oup.com/rfs/article-abstract/33/5/2019/5236964) | 65% of 452 anomalies fail replication, 82% under a multiple-testing hurdle, 96% in the trading-frictions category | Context for reporting a negative result |
| [Avramov, Cheng & Metzker (2023), *Management Science*](https://pubsonline.informs.org/doi/abs/10.1287/mnsc.2022.4449) | ML profitability concentrates in hard-to-arbitrage assets and decays with realistic costs | CL front-month is the least likely place for this edge to survive |
| [Arnott, Harvey & Markowitz (2019), *JFDS*](https://people.duke.edu/~charvey/Research/Published_Papers/P138_A_backtesting_protocol.pdf) | A backtesting protocol for machine learning, including pre-registration | The cheapest reform available to us |
| [Azevedo, Hoegner & Velikov, SSRN 4702406](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4702406) **(WP)** | Accounting for costs and decay, sophisticated ML strategies do earn net out-of-sample returns | Included for balance: "ML never works" is also overstated |

---

## Reading order for someone new

1. [Fletcher, Hussain & Shawe-Taylor (2010)](https://proceedings.mlr.press/v11/fletcher10a.html) — what this kind of model looks like, free to read.
2. [Nagel (2025)](https://www.nber.org/papers/w34104) — why our own numbers behave as they do.
3. [Harvey et al. (2018)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3175538) — what volatility sizing does and does not buy.
4. [Ilmanen & Kizer (2012)](https://www.aqr.com/-/media/AQR/Documents/Journal-Articles/JPM-The-Death-of-Diversification-Greatly-Exaggerated.pdf) — why the contract screen is built around decorrelation.
