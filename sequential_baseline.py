import time
import dataclasses
import re  # Built-in regular expressions: search, match, and extract text patterns
import json
import logging
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from dataclasses import dataclass
from urllib.parse import urljoin

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

def fetch(url,session, retries=5, base_delay=1 ): 
   for attempt in range(1,retries+1):
        delay=base_delay*(2**(attempt-1))
        try:
            response=session.get(url, timeout=10)  # 2. This request accepts a cokkie from server and automatically sends that same cookie back and gets a response object
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
                  time.sleep(delay)

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
                    
        except requests.exceptions.InvalidURL:
                display_error("Invalid Url \n please check your link")
                return None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as err: #requests.exceptions.Timeout  this is read timeout connectione happened but there is no answer so read timed out
                
                display_error(f"Network error on attempt {attempt}: {err}. Retrying in {delay}s...")
                if attempt != retries:
                 time.sleep(delay)
        except requests.exceptions.RequestException as req:
                display_error(f"Unexpected Error \n {req}")
                return None
   display_error(f"Failed to fetch {url} after {retries} attempts.")
   return None


def parse_generator(response,stats):

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
        
        yield dataclasses.asdict(book)
    



def writer(data,file):
    file.write(json.dumps(data) + "\n")



def main():
  t1=time.perf_counter()
  stats={"parsed":0 , "skipped":0, "failed_pages":[]}
  with requests.session() as session:
        url="https://books.toscrape.com/catalogue/page-1.html"
        response=fetch(url,session)
        if response is None:
            stats['failed_pages'].append(url)
            logging.error("could not fetch page 1, aborting")
            return
        doc=BeautifulSoup(response.text, "html.parser")
        total_page=doc.select_one("li.current")
        total_page=tag_not_found(total_page,"total_page").split()[-1]  
        url_generator_object=generate_urls(int(total_page))  #calling a generator returns a generator object and you wake it using a for loop like below or by expicitly saying next or typecast it to a list.  
        directory=Path(__file__).resolve().parent
        file_path=directory/"data"/"data.jsonl"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open (file_path, 'w', encoding="utf-8") as file:
            for url in url_generator_object:
                proccess_page(url,stats,session,file)       
  t2=time.perf_counter() 
  t=t2-t1          
  logging.info(f"The scraper has completed!\n\n number of parsed books: {stats['parsed']} \n\n number of skipped tasks: {stats['skipped']}\n\n number of failed pages is {len(stats['failed_pages'])} \n\n with url list {stats['failed_pages']} \n\n the time taken to complete this sync program is {t}")
  
def proccess_page(url,stats,session,file):
        response=fetch(url,session)
        if response is None:
            stats['failed_pages'].append(url)
            return
        data_generator_object=parse_generator(response, stats)
        for data in data_generator_object:
             writer(data, file)
        
if __name__ =="__main__":
    main()
# this program took around 20.989435099996626 seconds to completly scrape 1000 pages i will make it async and will see the improvement



