import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx


@dataclass
class Book:
    title: str
    price: float
    rating: int
    link: str
    availability: bool

    @classmethod
    def from_raw(cls, title, price, rating, link, availability):
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("There is no title")

        match = re.search(r"\d+(?:\.\d+)?", price)
        if match is None:
            raise ValueError(f"no number found in {price!r}")
        clean_price = float(match.group())

        rating_matching = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5
        }
        clean_rating = rating_matching.get(rating.lower())
        if clean_rating is None:
            raise ValueError(f"No valid rating {rating}")

        base_url = "https://books.toscrape.com/catalogue/"
        relative_url = link
        clean_url = urljoin(base_url, relative_url)

        clean_availability = "in stock" in availability.lower()

        return cls(clean_title, clean_price, clean_rating, clean_url, clean_availability)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def generate_urls(total_page):
    for n in range(1, total_page + 1):
        yield f"https://books.toscrape.com/catalogue/page-{n}.html"


def display_error(msg):
    logging.warning(msg)


def tag_not_found(tag, field):
    if tag is None:
        raise ValueError(f"{field} is missing.")
    return tag.get_text(strip=True)


async def fetch(stats, client, url_que, http_que, retries=5, base_delay=1):
    while True:
        url = await url_que.get()
        try:
            for attempt in range(1, retries + 1):
                delay = base_delay * (2 ** (attempt - 1))
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        await http_que.put(response)
                        break
                    elif response.status_code == 429 or response.status_code >= 500:
                        match response.status_code:
                            case 429:
                                display_error(f"Too Many Requests: Rate-limiting triggered. Retrying in {delay} seconds")
                            case 500:
                                display_error(f"Internal Server Error. Retrying in {delay} seconds")
                            case 502:
                                display_error(f"Bad Gateway. Retrying in {delay} seconds")
                            case 503:
                                display_error(f"Service Unavailable. Retrying in {delay} seconds")
                            case 504:
                                display_error(f"Gateway Timeout. Retrying in {delay} seconds")
                            case _:
                                display_error(f"Unexpected Server Error. Retrying in {delay} seconds")
                        if attempt != retries:
                            await asyncio.sleep(delay)

                    elif 400 <= response.status_code < 500:
                        match response.status_code:
                            case 400:
                                display_error("Bad Request Payload")
                            case 401:
                                display_error("Unauthorized: Invalid credentials")
                            case 403:
                                display_error("Forbidden: Access denied")
                            case 404:
                                display_error("Not Found: URL does not exist")
                            case 405:
                                display_error("Method Not Allowed")
                            case 408:
                                display_error("Request Timeout")
                            case _:
                                display_error("Unexpected Client Error")
                        stats['failed_pages'].append(url)
                        break
                    else:
                        display_error("Unexpected status error! Exiting retry loop...")
                        stats['failed_pages'].append(url)
                        break

                except httpx.InvalidURL:
                    display_error("Invalid URL: Please check the link")
                    stats['failed_pages'].append(url)
                    break
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt != retries:
                        await asyncio.sleep(delay)
                except httpx.RequestError as req:
                    display_error(f"Unexpected Request Error: {req}")
                    stats['failed_pages'].append(url)
                    break
            else:
                display_error(f"Failed to fetch {url} after {retries} attempts.")
                stats['failed_pages'].append(url)
                continue

        finally:
            url_que.task_done()


async def parser(stats, que, http_que):
    while True:
        response = await http_que.get()
        try:
            if response is None:
                continue
            doc = BeautifulSoup(response.content, "html.parser")
            articles = doc.select("article.product_pod")

            if not articles:
                display_error("Page has no books, layout may have changed!")
                continue
            for article in articles:
                try:
                    title = article.h3.a["title"]
                    price = article.select_one("p.price_color")
                    price = tag_not_found(price, "price")
                    rating = article.select_one("p.star-rating")['class'][1]
                    link = article.h3.a['href']
                    availability = article.select_one("p.instock")
                    availability = tag_not_found(availability, "availability")
                    book = Book.from_raw(title, price, rating, link, availability)
                    logging.debug(f"Parsing has succeeded for book with url {link}")
                    stats['parsed'] += 1
                except (ValueError, IndexError, TypeError, KeyError, AttributeError) as err:
                    display_error(f"An error occurred: {err} while parsing a book; skipping")
                    stats['skipped'] += 1
                    continue

                await que.put(asdict(book))
        finally:
            http_que.task_done()


async def writer(file, que):
    while True:
        try:
            record = await que.get()
            file.write(json.dumps(record) + "\n")
        finally:
            que.task_done()


async def main():
    t1 = time.perf_counter()
    stats = {"parsed": 0, "skipped": 0, "failed_pages": []}
    url_que = asyncio.Queue(maxsize=50)
    que = asyncio.Queue(maxsize=100)
    http_que = asyncio.Queue(maxsize=5)

    async with httpx.AsyncClient(timeout=15) as client:
        url = "https://books.toscrape.com/catalogue/page-1.html"
        response = await fetcher_page1(url, client)
        if response is None:
            stats['failed_pages'].append(url)
            logging.error("Could not fetch page 1, aborting")
            return

        doc = BeautifulSoup(response.text, "html.parser")
        total_page = doc.select_one("li.current")
        total_page = tag_not_found(total_page, "total_page").split()[-1]
        url_generator_object = generate_urls(int(total_page))

        directory = Path(__file__).resolve().parent
        file_path = directory / "data" / "async_data.jsonl"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, 'w', encoding="utf-8") as file:
            writer_task = asyncio.create_task(writer(file, que))
            fetcher_workers = [asyncio.create_task(fetch(stats, client, url_que, http_que)) for _ in range(4)]
            parser_workers = [asyncio.create_task(parser(stats, que, http_que)) for _ in range(4)]

            for url in url_generator_object:
                await url_que.put(url)

            # 1. Wait until all URLs are fetched
            await url_que.join()
            for w in fetcher_workers:
                w.cancel()

            # 2. Wait until all HTML pages are parsed
            await http_que.join()
            for w in parser_workers:
                w.cancel()

            # 3. Wait until all books are saved to disk
            await que.join()
            writer_task.cancel()

            await asyncio.gather(*fetcher_workers, return_exceptions=True)
            await asyncio.gather(*parser_workers, return_exceptions=True)
            await asyncio.gather(writer_task, return_exceptions=True)

        t2 = time.perf_counter()
        t = t2 - t1
        logging.info(
            f"The scraper has completed!\n\n"
            f" number of parsed books: {stats['parsed']}\n\n"
            f" number of skipped tasks: {stats['skipped']}\n\n"
            f" number of failed pages is {len(stats['failed_pages'])}\n\n"
            f" with url list {stats['failed_pages']}\n\n"
            f" the time taken to complete this async program is {t:.2f}s"
        )


async def fetcher_page1(url, client):
    try:
        response = await client.get(url)
        return response if response.status_code == 200 else None
    except httpx.RequestError as req:
        display_error(f"Unexpected Error on page 1: {req}")
        return None


if __name__ == "__main__":
    asyncio.run(main())