# Data source notes and decisions

Last updated: 2026-09-27

## Current source: SerpApi free plan
- Google Travel Explore: origin only, about 40 priced airports per search. International
  destinations need a separate search per region, and interest filters (Outdoors, Skiing,
  Beaches) return different lists.
- Google Flights Deals: about 30 deals per search, with Google's "usual price" and % off.
- 250 searches/month free, renewing on the 28th. Our daily schedule uses about 165.
- Risk: Google is suing SerpApi over scraping, so the service could go away. Every observation is
  stored locally (`output/serpapi/fares.db`), so our history survives either way.

## Ruled out (2026-09)
| Source | Why |
|---|---|
| Travelpayouts / Aviasales Data API | An API token needs a verified website with their "Drive" script installed |
| KAYAK Affiliate API | Business partnership only; they assess website and traffic |
| Amadeus Self-Service | Shut down 2026-07-17 |
| Kiwi Tequila | Invite-only for business partners |
| Skyscanner, Sabre, Travelport | Commercial contracts required |
| SerpApi paid, SearchApi.io, FlightAPI.io | $25–49/month, over budget |
| Duffel | Charges for searches when there are no bookings; built for booking |
| Ignav | One route, one date per request; no "anywhere" or date-range search; data source not disclosed. 1,000 free requests (one-time), then $2 per 1,000. Could be used to check a flagged deal |

## Possible fallback: scraping Google Flights ourselves (NOT built)

We may fall back to our own low-frequency browser automation (e.g. Playwright) of the
**Google Travel Explore page** if either of these happens:
- the SerpApi 250-searches/month cap becomes the bottleneck (we want more regions, trip lengths,
  months or interest filters than the free tier allows), or
- SerpApi stops working or becomes unaffordable.

**What we checked (2026-09-27):**
- Google's terms prohibit automated access that goes against machine-readable instructions such as
  robots.txt.
- google.com/robots.txt (User-agent: *) **disallows** `/travel/flights/search`,
  `/travel/flights/s/`, `/travel/flights/booking` and `/travel/search`.
- It does **not** disallow `/travel/explore` or `/travel/flights?...` result URLs.
- Re-check robots.txt and Google's terms before building. Both can change.

**Rules if we build it:**
1. Read the rendered **Explore page** only. Don't build against Google's undocumented internal
   data endpoints; they change without warning and are a grayer area.
2. Low frequency only: a few page loads per day, paced, never around the clock.
3. If a CAPTCHA or "unusual traffic" page appears, **stop the run** and log it. Never try to get
   around bot checks. That is what Google is suing SerpApi over, and it could also get the home
   connection flagged in everyday Google use.
4. Write into the same `fare_observations` table (with `source = 'google_explore_browser'`) so the
   history stays continuous.
5. Expect breakage when Google changes its page. Keep the extraction code small and isolated.

Open-source libraries such as `fli` and `fast-flights` use Google's internal endpoints and don't
cover Explore, so they don't meet rule 1. They're fine at most for occasional manual spot checks.
