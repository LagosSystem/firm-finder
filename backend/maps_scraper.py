import sys
import threading
import asyncio
import re
import urllib.parse
from typing import AsyncGenerator, Dict, Any, Optional, Callable, Tuple
from playwright.async_api import async_playwright, Page, BrowserContext

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

async def dismiss_consent_if_present(page: Page):
    """Dismiss Google consent modal if presented."""
    try:
        selectors = [
            'button:has-text("Accept all")',
            'button:has-text("I agree")',
            'button:has-text("Agree")',
            'form button[aria-label*="Accept"]',
            'button[aria-label*="Accept all"]'
        ]
        for sel in selectors:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await asyncio.sleep(1)
                break
    except Exception:
        pass

def parse_phone(text: str) -> Optional[str]:
    """Extract standard phone number format."""
    if not text:
        return None
    match = re.search(r'(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})', text)
    if match:
        return match.group(0).strip()
    return None

def parse_rating(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r'([1-5]\.[0-9])', text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None

def parse_reviews_count(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r'\(?([0-9,]+)\)?', text)
    if match:
        try:
            clean = match.group(1).replace(',', '')
            return int(clean)
        except ValueError:
            return None
    return None

def clean_address_and_category(snippets_text: str, location_query: str = "") -> Tuple[str, str, str, str, str]:
    """
    Cleans raw card snippets and separates:
    (clean_street_address, city, state, postal_code, category)
    Guaranteed: NEVER contains 'Open/Closed', hours, phone numbers, or ratings.
    """
    clean = re.sub(r'(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})', '', snippets_text)
    clean = re.sub(r'(Closed|Open 24 hours|Opens?\s+[0-9:\sAPMapm]+[a-zA-Z]*|Closes?\s+[0-9:\sAPMapm]+[a-zA-Z]*|Open\b|Fermé[e]?|Ouvert[e]?|Ouvre à\s+[0-9h:]+|Ferme à\s+[0-9h:]+|24h/24)', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'[1-5]\.[0-9]\s*\([0-9,]+\)', '', clean)
    clean = re.sub(r'[\uE000-\uF8FF]', '', clean) # remove private-use icon glyphs
    clean = re.sub(r'[\r\n|]+', ' · ', clean)

    raw_parts = [p.strip() for p in clean.split('·') if p.strip()]

    category = ""
    street = ""

    for part in raw_parts:
        if not category and not re.search(r'\d{2,}', part):
            # First text part without numbers is typically category (e.g. 'Estate Planning Lawyer')
            category = part
        elif not street and re.search(r'\d+\s+[A-Za-z]', part):
            street = part

    # If street wasn't found by digit pattern, look for remaining parts
    if not street and len(raw_parts) > 1:
        for part in raw_parts:
            if part != category and len(part) > 3:
                street = part
                break

    city = ""
    state = ""
    postal = ""

    # Parse Canadian postal code if present (e.g. G1K 7A4, H3B 2Y5)
    ca_postal_m = re.search(r'\b([A-Za-z]\d[A-Za-z]\s*\d[A-Za-z]\d)\b', street)
    if ca_postal_m:
        postal = ca_postal_m.group(1).upper()
        street = street[:ca_postal_m.start()].strip()

    # Parse City, State, Zip if full address in street string (US format)
    m = re.search(r',\s*([A-Za-z\s]+),\s*([A-Z]{2})\s*(\d{5})?', street)
    if m:
        city = m.group(1).strip()
        state = m.group(2).strip()
        if not postal:
            postal = m.group(3).strip() if m.group(3) else ""
        street = street[:m.start()].strip()
    else:
        # Fallback from location_query (e.g. 'Fulton County, Atlanta, GA' or 'Quebec, Canada')
        loc_parts = [p.strip() for p in location_query.split(',') if p.strip()]
        for p in loc_parts:
            if len(p) == 2 and p.isupper():
                state = p
            elif 'county' not in p.lower() and not city:
                city = p

    return street, city, state, postal, category

def evaluate_relevance(name: str, category: str, query: str) -> Tuple[str, str]:
    """
    Evaluates whether the listing looks like a genuine match for the search query.
    Returns (relevance: 'high' | 'low', reason: str).
    """
    q = query.lower()
    c = (category or "").lower()
    n = name.lower()

    if any(k in q for k in ['law', 'attorney', 'legal', 'lawyer', 'avocat', 'droit', 'juridique']):
        low_cats = ['government', 'city hall', 'police', 'court', 'department', 'school', 'church', 'holding company', 'bank', 'bail bonds', 'jail', 'gouvernement', 'tribunal', 'palais de justice', 'mairie']
        if any(bad in c for bad in low_cats):
            return "low", f"Category is '{category}' (non-firm entity)"
        high_cats = ['law', 'attorney', 'lawyer', 'legal', 'counsel', 'litigation', 'justice', 'estate planning', 'injury', 'defense', 'corporate', 'trial', 'solicitor', 'avocat', 'avocate', 'cabinet', 'droit', 'notaire', 'juridique']
        if any(good in c or good in n for good in high_cats):
            return "high", "Relevant legal practice"
        return "low", f"Category '{category}' is not a recognized legal practice"

    if any(k in q for k in ['private equity', 'equity', 'venture capital', 'investment', 'capital']):
        low_cats = ['law firm', 'attorney', 'government', 'restaurant', 'store', 'medical', 'auto', 'contractor', 'repair', 'barber', 'salon']
        if any(bad in c for bad in low_cats):
            return "low", f"Category is '{category}'"
        high_cats = ['private equity', 'venture', 'investment', 'capital', 'fund', 'holding company', 'asset management', 'financial', 'securities']
        if any(good in c or good in n for good in high_cats):
            return "high", "Relevant investment entity"
        return "low", f"Category '{category}' does not match investment/private equity"

    # Default general match
    return "high", "Matches search keyword"

async def scrape_google_maps(
    query: str,
    location: str,
    max_results: int = 25,
    is_cancelled: Optional[Callable[[], bool]] = None,
    log_callback: Optional[Callable[[str], None]] = None
) -> Tuple[AsyncGenerator[Dict[str, Any], None], Callable[[], str]]:
    """
    Asynchronously scrape Google Maps for business listings matching query & location.
    Runs browser automation in an isolated Proactor event loop to guarantee 100% subprocess
    compatibility across Windows, Uvicorn reloader, and Python 3.14.
    """
    search_term = f"{query} in {location}".strip()
    encoded_search = urllib.parse.quote_plus(search_term)
    maps_url = f"https://www.google.com/maps/search/{encoded_search}?hl=en"

    main_loop = asyncio.get_running_loop()
    bridge_queue: asyncio.Queue = asyncio.Queue()
    end_state = {"reason": "search_exhausted", "total_found": 0}

    def get_end_reason() -> str:
        return end_state["reason"]

    def emit_log(msg: str):
        if log_callback:
            main_loop.call_soon_threadsafe(log_callback, msg)

    async def _internal_scrape_worker():
        emit_log(f"Initiating directory lookup for '{query}' in '{location}'...")

        async with async_playwright() as p:
            browser = None
            for channel_choice in ["chrome", "msedge", None]:
                try:
                    kwargs = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--mute-audio",
                            "--disable-extensions",
                            "--disable-default-apps",
                            "--disable-blink-features=AutomationControlled",
                            "--window-size=1280,900"
                        ]
                    }
                    if channel_choice:
                        kwargs["channel"] = channel_choice
                    browser = await p.chromium.launch(**kwargs)
                    break
                except Exception:
                    continue

            if not browser:
                end_state["reason"] = "error"
                emit_log("System browser unavailable. Execution terminated.")
                return

            context: BrowserContext = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US"
            )

            page = await context.new_page()

            # Block heavy images, media, and fonts to save 75%+ RAM and accelerate loading
            async def block_heavy_assets(route):
                if route.request.resource_type in ["image", "media", "font"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_heavy_assets)

            yielded_count = 0
            try:
                emit_log("Connecting to directory search index...")

                try:
                    await page.goto(maps_url, timeout=25000, wait_until="domcontentloaded")
                except Exception:
                    end_state["reason"] = "timeout"
                    emit_log("Connection timed out loading directory search index.")
                    return

                await asyncio.sleep(1)
                await dismiss_consent_if_present(page)

                # Check if rate-limited or captcha
                page_content = await page.content()
                if "unusual traffic from your computer network" in page_content.lower() or "solving the above challenge" in page_content.lower():
                    end_state["reason"] = "rate_limited"
                    emit_log("Search index temporarily rate-limited.")
                    return

                feed_selector = 'div[role="feed"]'
                try:
                    await page.wait_for_selector(f'{feed_selector}, div.Nv2PK, div[role="main"]', timeout=12000)
                except Exception:
                    end_state["reason"] = "search_exhausted"
                    emit_log(f"Search exhausted: 0 matches found (target was {max_results}).")
                    return

                has_feed = await page.locator(feed_selector).count() > 0 or await page.locator('div.Nv2PK').count() > 0

                # Single direct place result
                if not has_feed:
                    single_firm = await extract_single_place(page, search_term, query, location)
                    if single_firm:
                        end_state["total_found"] = 1
                        end_state["reason"] = "target_reached" if max_results == 1 else "search_exhausted"
                        main_loop.call_soon_threadsafe(bridge_queue.put_nowait, single_firm)
                    return

                processed_names = set()
                yielded_count = 0
                stagnant_scroll_count = 0
                last_card_count = 0

                # Position mouse cursor over the feed container to ensure wheel events scroll the listing feed
                feed_el = page.locator('div[role="feed"]').first
                feed_box = await feed_el.bounding_box() if await feed_el.count() > 0 else None
                if feed_box:
                    await page.mouse.move(feed_box['x'] + feed_box['width'] / 2, feed_box['y'] + feed_box['height'] / 2)

                while yielded_count < max_results:
                    if is_cancelled and is_cancelled():
                        end_state["reason"] = "stopped"
                        emit_log(f"Search stopped by user: {yielded_count} matches found.")
                        break

                    cards = page.locator('div.Nv2PK')
                    count = await cards.count()

                    for i in range(count):
                        if is_cancelled and is_cancelled():
                            end_state["reason"] = "stopped"
                            break
                        if yielded_count >= max_results:
                            end_state["reason"] = "target_reached"
                            break

                        card = cards.nth(i)
                        try:
                            title_el = card.locator('div.fontHeadlineSmall, div.qBF1Pd, div.NrDZNb').first
                            link_el = card.locator('a.hfpxzc').first

                            name = ""
                            if await title_el.count() > 0:
                                name = (await title_el.text_content() or "").strip()
                            if not name and await link_el.count() > 0:
                                name = (await link_el.get_attribute('aria-label') or "").strip()

                            if not name or name in processed_names:
                                continue

                            processed_names.add(name)
                            maps_link = (await link_el.get_attribute('href') or "").strip() if await link_el.count() > 0 else ""

                            # Rating & reviews
                            card_text = await card.text_content() or ""
                            rating = parse_rating(card_text)
                            reviews_count = parse_reviews_count(card_text)

                            # Parse snippet elements for phone, address, and category
                            snippets = card.locator('div.W4Efsd')
                            scnt = await snippets.count()
                            combined_snippet_text = ""
                            for s_idx in range(scnt):
                                st = (await snippets.nth(s_idx).inner_text() or "").strip()
                                if st:
                                    combined_snippet_text += " · " + st

                            phone = parse_phone(combined_snippet_text) or parse_phone(card_text)
                            street, city, state, postal, category = clean_address_and_category(combined_snippet_text, location)

                            # Evaluate relevance
                            relevance, relevance_reason = evaluate_relevance(name, category, query)

                            # Website extraction
                            website = None
                            web_link = card.locator(
                                'a[data-value="Website"], a[aria-label*="website" i], a[aria-label*="site web" i], a[aria-label*="sitio web" i], a[data-tooltip*="website" i], a[href^="http"]:not([href*="google."]):not([href*="goo.gl"])'
                            ).first
                            if await web_link.count() > 0:
                                raw_web = await web_link.get_attribute('href')
                                if raw_web:
                                    if raw_web.startswith('/'):
                                        raw_web = "https://www.google.com" + raw_web
                                    if "google.com" in raw_web or "google." in raw_web:
                                        parsed_g = urllib.parse.urlparse(raw_web)
                                        qs = urllib.parse.parse_qs(parsed_g.query)
                                        target_q = qs.get("q", [None])[0] or qs.get("url", [None])[0] or qs.get("adurl", [None])[0]
                                        if target_q and target_q.startswith("http"):
                                            website = target_q.strip()
                                        else:
                                            website = raw_web.strip()
                                    else:
                                        website = raw_web.strip()

                            place_id = None
                            if maps_link:
                                pid_match = re.search(r'!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)', maps_link)
                                if pid_match:
                                    place_id = pid_match.group(1)

                            firm_dict = {
                                "search_query": search_term,
                                "name": name,
                                "category": category or "Directory Listing",
                                "relevance": relevance,
                                "relevance_reason": relevance_reason,
                                "phone": phone,
                                "website": website,
                                "address": street,
                                "city": city,
                                "state": state,
                                "postal_code": postal,
                                "google_maps_url": maps_link or page.url,
                                "place_id": place_id or f"place_{abs(hash(name + str(phone)))}",
                                "rating": rating,
                                "reviews_count": reviews_count,
                                "enrichment_status": "pending"
                            }

                            yielded_count += 1
                            end_state["total_found"] = yielded_count

                            emit_log(f"Found match #{yielded_count}: {name} [{category or 'Firm'}] (Phone: {phone or 'N/A'})")
                            main_loop.call_soon_threadsafe(bridge_queue.put_nowait, firm_dict)

                        except Exception:
                            continue

                    if yielded_count >= max_results:
                        end_state["reason"] = "target_reached"
                        break

                    # Check for explicit end of results marker in Google Maps feed
                    end_marker = page.locator('span:has-text("You\'ve reached the end of the list"), div:has-text("You\'ve reached the end of the list")')
                    if await end_marker.count() > 0 and await end_marker.first.is_visible():
                        end_state["reason"] = "search_exhausted"
                        break

                    # Scroll feed container using last card into view + mouse wheel
                    try:
                        await cards.last.scroll_into_view_if_needed(timeout=1000)
                    except Exception:
                        pass
                    await page.mouse.wheel(0, 4500)
                    await asyncio.sleep(1.5)

                    new_count = await cards.count()
                    if new_count == last_card_count:
                        stagnant_scroll_count += 1
                        if stagnant_scroll_count >= 6:
                            end_state["reason"] = "search_exhausted"
                            break
                    else:
                        stagnant_scroll_count = 0
                    last_card_count = new_count

            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

                if end_state["reason"] == "target_reached":
                    msg = f"Target reached: {yielded_count} matches found."
                elif end_state["reason"] == "search_exhausted":
                    msg = f"Search exhausted: {yielded_count} matches found (target was {max_results})."
                elif end_state["reason"] == "stopped":
                    msg = f"Search paused by user: {yielded_count} matches found."
                elif end_state["reason"] == "rate_limited":
                    msg = f"Search index rate-limited after {yielded_count} matches."
                else:
                    msg = f"Search ended with status: {end_state['reason']} ({yielded_count} matches)."

                emit_log(msg)

    def proactor_thread_runner():
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_internal_scrape_worker())
        except Exception as e:
            import traceback
            print(f"[Scraper Proactor Thread Exception]:\n{traceback.format_exc()}")
            end_state["reason"] = "error"
            emit_log(f"Scraper error: {str(e) or type(e).__name__}")
        finally:
            main_loop.call_soon_threadsafe(bridge_queue.put_nowait, None) # Sentinel to close consumer
            loop.close()

    thread = threading.Thread(target=proactor_thread_runner, daemon=True)
    thread.start()

    async def listing_generator() -> AsyncGenerator[Dict[str, Any], None]:
        while True:
            item = await bridge_queue.get()
            if item is None:
                break
            yield item

    return listing_generator(), get_end_reason

async def extract_single_place(page: Page, search_term: str, query: str, location: str) -> Optional[Dict[str, Any]]:
    """Extract information when Google Maps directly opens a single entity."""
    try:
        title_el = page.locator('h1.DUwDvf').first
        name = (await title_el.text_content() or "").strip()
        if not name:
            return None

        phone_el = page.locator('button[data-item-id*="phone:"], button[aria-label*="phone" i]').first
        phone = None
        if await phone_el.is_visible(timeout=1000):
            phone = parse_phone(await phone_el.text_content() or "")

        web_el = page.locator('a[data-item-id="authority"], a[aria-label*="website" i]').first
        website = None
        if await web_el.is_visible(timeout=1000):
            website = (await web_el.get_attribute('href') or "").strip()

        addr_el = page.locator('button[data-item-id*="address"], button[aria-label*="address" i]').first
        raw_addr = ""
        if await addr_el.is_visible(timeout=1000):
            raw_addr = (await addr_el.text_content() or "").strip()

        street, city, state, postal, category = clean_address_and_category(raw_addr, location)
        relevance, relevance_reason = evaluate_relevance(name, category, query)

        return {
            "search_query": search_term,
            "name": name,
            "category": category or "Directory Listing",
            "relevance": relevance,
            "relevance_reason": relevance_reason,
            "phone": phone,
            "website": website,
            "address": street,
            "city": city,
            "state": state,
            "postal_code": postal,
            "google_maps_url": page.url,
            "place_id": f"single_{abs(hash(name))}",
            "rating": None,
            "reviews_count": None,
            "enrichment_status": "pending"
        }
    except Exception:
        return None
