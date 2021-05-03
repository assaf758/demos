# Generated by nuclio.export.NuclioExporter

from bs4 import BeautifulSoup as bs
from urllib.request import Request, urlopen
import requests
import pandas as pd
import v3io_frames as v3f
from unicodedata import normalize
from datetime import datetime
import re
import os
import json
import ast
import mlrun.feature_store as fs
from mlrun.feature_store.steps import *
import mlrun

mlrun.set_environment(project="stocks")


def get_stock_news_page(stock_string):
    request = Request('https://www.investing.com/equities/' + stock_string + '-news',
                      headers={"User-Agent": "Mozilla/5.0"})
    content = urlopen(request).read()
    return bs(content, 'html.parser')


def get_internal_article_links(page):
    news = page.find_all('div', attrs={'class': 'mediumTitle1'})[1]
    articles = news.find_all('article', attrs={'class': 'js-article-item articleItem'})
    return ['https://www.investing.com' + a.find('a').attrs['href'] for a in articles]


def get_article_page(article_link):
    request = Request(article_link, headers={"User-Agent": "Mozilla/5.0"})
    content = urlopen(request).read()
    return bs(content, 'html.parser')


def clean_paragraph(paragraph):
    paragraph = re.sub(r'\(http\S+', '', paragraph)
    paragraph = re.sub(r'\([A-Z]+:[A-Z]+\)', '', paragraph)
    paragraph = re.sub(r'[\n\t\s\']', ' ', paragraph)
    return normalize('NFKD', paragraph)


def extract_text(article_page):
    text_tag = article_page.find('div', attrs={'class': 'WYSIWYG articlePage'})
    paragraphs = text_tag.find_all('p')
    text = '\n'.join([clean_paragraph(p.get_text()) for p in paragraphs[:-1]])
    return text


import json


def get_publish_time(article):
    tag = article.find('script', {"type": "application/ld+json"}).contents[0]
    tag_dict = json.loads(str(tag))
    dateModified = tag_dict["dateModified"]
    return datetime.strftime(datetime.strptime(dateModified, '%Y-%m-%d %H:%M:%S'), '%Y-%m-%d %H:%M:%S')


def get_score(paragraph_scores):
    return sum([score - 1 for score in paragraph_scores['outputs']]) / len(paragraph_scores)


def get_article_scores(context, articles, endpoint):
    scores = []
    for i, article in enumerate(articles):
        context.logger.info(f'getting score for article {i + 1}\\{len(articles)}')
        event_data = {'inputs': article.split('\n')}
        resp = requests.put(endpoint, json=json.dumps(event_data))
        scores.append(get_score(json.loads(resp.text)))
    return scores


def construct_dataframe(sentiments, items):
    tickers = [x[0] for x in items]
    stock_sent = pd.DataFrame({"symbol": tickers, "sentiment": sentiments})
    return stock_sent


def init_context(context):
    context.logger.info("init context")

    mlrun.set_environment(project="stocks-avia")
    stocks_sent_set = fs.FeatureSet("stock_sent", entities=[fs.Entity("symbol")])
    setattr(context, 'stock_feature_set', stocks_sent_set)

    v3io_framesd = os.getenv('V3IO_FRAMESD', 'framesd:8081')
    token = os.getenv('TOKEN', '')
    client = v3f.Client(v3io_framesd, container=os.getenv('V3IO_CONTAINER', 'bigdata'), token=token)
    setattr(context, 'v3c', client)

    setattr(context, 'stocks_stream', os.getenv('STOCKS_STREAM', 'stocks/stocks_stream'))
    context.v3c.create(backend='stream', table=context.stocks_stream, if_exists=1)

    setattr(context, 'stocks_tsdb', os.getenv('STOCKS_TSDB_TABLE', 'stocks/stocks_tsdb'))
    context.v3c.create(backend='tsdb', table=context.stocks_tsdb, rate='1/s', if_exists=1)

    setattr(context, 'sentiment_model_endpoint',
            os.getenv('SENTIMENT_MODEL_ENDPOINT', 'http://nuclio-stocks-sentiment-analysis-server:8080'))
    context.logger.info(f"set sentiment_model_endpoint {context.sentiment_model_endpoint}")
    sym_to_url = {'GOOGL': 'google-inc', 'MSFT': 'microsoft-corp', 'AMZN': 'amazon-com-inc',
                  'AAPL': 'apple-computer-inc'}
    setattr(context, 'sym_to_url', sym_to_url)
    setattr(context, 'stocks_kv', os.getenv('STOCKS_KV', 'stocks/stocks_kv'))
    context.logger.info('end init context')


def handler(context, event):
    context.logger.info(f'Getting news about {context.sym_to_url}')

    syms = []
    contents = []
    links = []
    times = []
    sentiments = []
    last_ticker_sentiment = []
    for sym, url_string in context.sym_to_url.items():
        context.logger.info(f'Getting news about {sym}')
        news_page = get_stock_news_page(url_string)
        article_links = get_internal_article_links(news_page)
        article_pages = [get_article_page(link) for link in article_links]
        articles = [extract_text(article_page) for article_page in article_pages]
        curr_sentiments = get_article_scores(context, articles, context.sentiment_model_endpoint)
        curr_times = [get_publish_time(article_page) for article_page in article_pages]

        sentiments += curr_sentiments
        times += curr_times
        for article, link, sentiment, time in zip(articles, article_links, curr_sentiments, curr_times):
            record = {
                'content': article,
                'time': time,
                'symbol': sym,
                'link': link,
                'sentiment': sentiment
            }
            context.v3c.execute('stream', context.stocks_stream, 'put', args={'data': json.dumps(record)})

            syms.append(sym)
            contents.append(article)
            links.append(link)
        context.v3c.execute('kv', context.stocks_kv, command='update', args={'key': sym,
                                                                             'expression': f'SET sentiment={sentiments[-1]}'})
        last_ticker_sentiment.append(sentiments[-1])
    stock_sent = construct_dataframe(last_ticker_sentiment, context.sym_to_url.items())
    context.logger.info(f'Ingesting new information to feature store')
    fs.ingest(context.stock_feature_set, stock_sent, infer_options=fs.InferOptions.default())

    if len(sentiments) > 0:
        df = pd.DataFrame.from_dict({'sentiment': sentiments,
                                     'time': times,
                                     'symbol': syms})
        df = df.set_index(['time', 'symbol'])
        df.index = df.index.set_levels([pd.to_datetime(df.index.levels[0]), df.index.levels[1]])
        df = df.sort_index(level=0, axis=0)
        context.v3c.write(backend='tsdb', table=context.stocks_tsdb, dfs=df)
