import dataclasses
import time
import httpx
import re  
import json
import logging
from pathlib import Path
from bs4 import BeautifulSoup
from dataclasses import dataclass
from urllib.parse import urljoin
import asyncio

@dataclass
class Book:
    title:str
    price: float
    rating: int
    link:str
    availability: bool

    @classmethod
    def from_raw(cls, title,price,rating,link,availability):
        
        clean_title=title.strip()
        if not clean_title:
            raise ValueError("There is no title")

        match=re.search(r"\d+(?:\.\d+)?", price)
        if match is None:
          raise ValueError(f"no number found in {price!r}")
        else:
          clean_price=float(match.group())

        ratting_matching={
            "one":1,
            "two":2,
            "three":3,
            "four":4,
            "five":5
        }
        clean_rating=ratting_matching.get(rating.lower())
        if clean_rating==None:
            raise ValueError(f"No valid rating {rating}")

        base_url="https://books.toscrape.com/catalogue/"
        relative_url=link
        clean_url=urljoin(base_url,relative_url)

        clean_availability="in stock" in availability.lower()

        return cls(clean_title,clean_price,clean_rating,clean_url,clean_availability)

logging.basicConfig(
    level=logging.INFO,                             # Set lowest severity level to capture
    format="%(asctime)s - %(levelname)s - %(message)s", # Structure of the output
   # filename="app.log",                              # Save logs to a file (instead of console)
   # filemode="w"                                     # 'w' to overwrite file, 'a' to append
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def generate_urls(total_page):
    for n in range(1, total_page +1):
        yield f"https://books.toscrape.com/catalogue/page-{n}.html"

def display_error(msg):
    logging.warning(msg)

def tag_not_found(tag,field):
    if tag is None:
        raise ValueError(f"{field} is missing.")
    else:
        tag=tag.get_text(strip=True)
        return tag

async def fetch(url,client, retries=5, base_delay=1 ): 
   for attempt in range(1,retries+1):
        delay=base_delay*(2**(attempt-1))
        try:
            response=await client.get(url) 
            if response.status_code==200: 
                  return response 
            elif response.status_code==429 or response.status_code>=500:
                
                match response.status_code:
                        case 429: display_error(f"Too Many Requests: \n  The user has triggered rate-limiting protections \n by sending too many requests too quickly.\n Retrying in {delay} seconds ")
                        case 500: display_error(f"Internal Server Error\n an unhandled generic exception or crash on the server. \n Retrying in {delay} seconds ")
                        case 502: display_error(f" Bad Gateway:\n An intermediary proxy or routing server received an invalid response from the upstream server. \n Retrying in {delay} seconds ")
                        case 503: display_error(f"Service Unavailable: \n The server is temporarily down for maintenance or crashing from high traffic volume. \n Retrying in {delay} seconds ")
                        case 504: display_error(f"Gateway Timeout:\nAn intermediary gateway timed out waiting for a response from the primary backend. \n Retrying in {delay} seconds ")
                        case _:   display_error(f"Unexpected Error. \n Retrying in {delay} seconds " )
                if attempt!=retries:
                  await asyncio.sleep(delay)

            elif 400<=response.status_code<500:
                match response.status_code:
                        case 400: display_error("Bad Request Payload \n The server cannot understand the request due to malformed syntax or corrupted data.")
                        case 401: display_error("Unauthorized\n The user lacks valid authentication credentials for the target resource.")
                        case 403: display_error("Forbidden \n The server understands who you are,\n but you do not have permission to access the data.")
                        case 404: display_error(" Not Found \n The server cannot locate \n the requested URL or resource.")
                        case 405: display_error("Method Not Allowed \n The request method (like POST or DELETE) is forbidden for this specific URL.")
                        case 408: display_error(" Request Timeout: \n The connection timed out because the client took too long to send a request.")
                        case _:   display_error("Unexpected Error")
                return None 
            else:
                display_error("unexpected status error! exiting...")
                return None 
                    
        except httpx.InvalidURL:
                display_error("Invalid Url \n please check your link")
                return None
        except (httpx.TimeoutException, httpx.NetworkError) as err: 
                if attempt != retries:
                 await asyncio.sleep(delay)
        except httpx.RequestError as req:
                display_error(f"Unexpected Error \n {req}")
                return None
   display_error(f"Failed to fetch {url} after {retries} attempts.")
   return None

async def parse_generator(response,stats,que):

      doc=BeautifulSoup(response.content, "html.parser")
      articles=doc.select("article.product_pod")

      if not articles:
        display_error("page has no books, layout may have changed!")
        return
      for article in articles:
        try:
            title=article.h3.a["title"]
            price=article.select_one("p.price_color")
            price=tag_not_found(price,"price")
            rating=article.select_one("p.star-rating")['class'][1]
            link=article.h3.a['href']
            availability=article.select_one("p.instock")
            availability=tag_not_found(availability,"availability")
            book=Book.from_raw(title,price,rating,link,availability) 
            logging.debug(f"parsing has succeded for a book with url {link}")
            stats['parsed']+=1
        except (ValueError, TypeError,KeyError,AttributeError)as err:
            display_error(f"An error occured: {err} on parsing a book, and it is skipped")
            stats['skipped']+=1
            continue
        
        await que.put(dataclasses.asdict(book)) 
    
async def writer(file,que):
  while True:
    record=await que.get()
    if record == "stop":
      que.task_done()
      break
      logging.info("writing is completed killing writter....")
    file.write(json.dumps(record) + "\n")
    que.task_done()


async def main():
  t1=time.perf_counter()
  stats={"parsed":0 , "skipped":0, "failed_pages":[]}
  url_que=asyncio.Queue(maxsize=50)
  que= asyncio.Queue(maxsize=100)
  async with httpx.AsyncClient(timeout=15) as client: 
        url="https://books.toscrape.com/catalogue/page-1.html"
        response= await fetcher_page1(url,client)
        if response is None:
            stats['failed_pages'].append(url)
            logging.error("could not fetch page 1, aborting")
            return
        
        doc=BeautifulSoup(response.text, "html.parser")
        total_page=doc.select_one("li.current")
        total_page=tag_not_found(total_page,"total_page").split()[-1]  
        url_generator_object=generate_urls(int(total_page))
        
        directory=Path(__file__).resolve().parent
        file_path=directory/"data"/"data.jsonl"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open (file_path, 'w', encoding="utf-8") as file:
            writter_task=asyncio.create_task(writer(file,que))
            fecher_worker=[asyncio.create_task(proccess_page(stats,client,que,url_que)) for _ in range(4)]

            for url in url_generator_object:
              await url_que.put(url)

            for _ in range(4):
             await url_que.put('stop')
            await asyncio.gather(*fecher_worker)
            
            await que.put("stop")

            await writter_task
        t2=time.perf_counter() 
        t=t2-t1          
        logging.info(f"The scraper has completed!\n\n number of parsed books: {stats['parsed']} \n\n number of skipped tasks: {stats['skipped']}\n\n number of failed pages is {len(stats['failed_pages'])} \n\n with url list {stats['failed_pages']} \n\n the time taken to complete this sync program is {t}")
        
async def proccess_page(stats,session,que,url_que):

  while True:
      url=await url_que.get()
      if url == "stop":
        url_que.task_done()
        break
        logging.info("Fetching and parsing Completed killing workers....")
      response= await fetch(url,session)
      url_que.task_done()
      if response is None:
        stats['failed_pages'].append(url)
        continue
      await parse_generator(response, stats,que)

async def fetcher_page1(url,client):
  response=await client.get(url)
  return response if response.status_code==200 else None
      
        
if __name__ =="__main__":
   asyncio.run(main())