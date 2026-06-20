from flask import Flask, jsonify, request
from flask_cors import CORS
import feedparser
import os
import firebase_admin
from firebase_admin import credentials, auth, firestore
import datetime
import random
import base64
import re
import requests
import unicodedata
import json
from bs4 import BeautifulSoup
import hashlib
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Firebase setup
cred = credentials.Certificate(r"./CredFile.json")
firebase_admin.initialize_app(cred)
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = r"./CredFile.json"

db = firestore.Client()


'''
All helper functions below
'''
def get_top_filtered_list(reads_dict,pref_list,top_n):
        # Filter out "Notmentioned" and "Nonementioned"
        filtered_items = {k: v for k, v in reads_dict.items() if k not in ["Notmentioned", "Nonementioned"]}
        # Sort items by count in descending order and get the top N keys
        top_read_ones=[k for k, v in sorted(filtered_items.items(), key=lambda item: item[1], reverse=True)[:top_n]]
        final_list=top_read_ones+pref_list
        return final_list


def verify_password(plain_password, hashed_password):
    return hashlib.sha256(plain_password.encode()).hexdigest() == hashed_password

def sanitize_name(name):
    """Sanitize the name by replacing non-alphanumeric characters with underscores"""
    name_ascii = name.encode('ascii', 'ignore').decode('ascii')  # Remove non-ASCII characters

    return re.sub(r'[^A-Za-z0-9]+', '', name_ascii)  # Remove non-alphanumeric characters entirely

def desanitize_name(sanitized_name):
    """
    Desanitize the name by adding spaces before uppercase letters,
    which might indicate word boundaries.
    """
    # Add spaces before uppercase letters (except the first letter)
    desanitized_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', sanitized_name)
    return desanitized_name


def get_user_preferences(user_id):
    user_ref = db.collection('users').document(user_id)
    print(user_ref)
    user_data = user_ref.get().to_dict() if user_ref.get().exists else {}
    return {
        'sport_reads': user_data.get('sport_reads', {}),
        'team_reads': user_data.get('team_reads', {}),
        'player_reads': user_data.get('player_reads', {}),
        'tournament_reads': user_data.get('tournament_reads', {})
    }

# Utility function to calculate score
def calculate_score(article_items, user_pref):
    return sum(user_pref.get(item, 0) for item in article_items)


def fetch_gemini_response(keyword):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key=AIzaSyASIxiKUuasdH49yXHy0grLBc6-Y-VKXKc"
    headers = {
        "Content-Type": "application/json"
    }
    body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": f"give me top 4 recent news summary about {keyword} in bullet format each point must be in 40 words , give me just bullet points as output. If you can't give any valuable information then just say one word no"
                    }
                ]
            }
        ]
    }

    response = requests.post(url, headers=headers, data=json.dumps(body))

    if response.status_code == 200:
        gemini_response = response.json()
        gem_text=gemini_response['candidates'][0]['content']['parts'][0]['text']
        bullet_points = [point.strip("- ").strip() for point in gem_text.splitlines() if point.strip()]
        updated_news = [news.replace('**', '<b>').replace('**', '</b>') for news in bullet_points]
        return bullet_points
    else:
        print(f"Error: {response.status_code}")
        print(response.text)
        return None


'''--------------------------------------------------------------------------------------------------------'''

'''All endpoints below'''
@app.route('/signup', methods=['POST'])
def signup():
    data = request.json
    username = data.get("username")
    email = data.get("email")
    password = data.get("password")
    preferences = data.get("preferences")
    teams = data.get("teams")
    tournaments = data.get("tournaments")
    players = data.get("players")

    preferences=[p.lower() for p in preferences]
    try:
        # Create the user in Firebase Authentication
        user = auth.create_user(
            email=email,
            password=password,
            display_name=username
        )

        # Save user preferences in Firestore

        user_data = {
            "username": username,
            "email": email,
            'password': password,
            "preferences": preferences,
            "tournaments":tournaments,
            "players":players,
            "teams":teams,
            "sport_reads":{},
            "player_reads":{},
            "team_reads":{},
            "tournament_reads":{}
        }
        db.collection("users").document(user.uid).set(user_data)

        # Send back user ID
        return jsonify({"uid": user.uid, "message": "User created successfully"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get("email")
    password = data.get("password")
    try:
        user = auth.get_user_by_email(email)
        
        user_doc = db.collection("users").document(user.uid).get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            stored_password = user_data.get("password")  
            if password == stored_password:
                preferences = user_data.get("preferences", [])
                preferences=[p.lower() for p in preferences]
                return jsonify({"uid": user.uid, "preferences": preferences,"tournaments":user_data.get("tournaments"), "players":user_data.get("players"), "teams":user_data.get("teams")}), 200
            else:
                return jsonify({"error": "Invalid password"}), 401
        else:
            return jsonify({"error": "User data not found"}), 404

    except firebase_admin.auth.UserNotFoundError:
        return jsonify({"error": "User not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


'''--------------------------------------------------------------------------------------------------------'''
'''core functionality:'''
def save_article_to_firebase(article, sport_type):
    # Check for existing articles by link to avoid duplicates
    articles_ref = db.collection('articles').where('link', '==', article['link']).get()
    if not articles_ref:
        article_summary = {
            "title": article['title'],
            "description": article['description'],
            "link": article['link'],
            "image": article['image_url'],
            "sport": sport_type,
            "time":article['published_date'],
            "players":[],
            "teams":[],
            "location":[],
            "stadium":[],
            "tournaments":[]
        }
        db.collection('articles').add(article_summary)


def parse_f1_rss(rss_url):


    feed = feedparser.parse(rss_url)
    articles = []

    for entry in feed.entries:
        # Parse and clean description with BeautifulSoup
        raw_description = entry.get("description", "No description available")
        description_text = BeautifulSoup(raw_description, "html.parser").get_text()

        article = {
            "title": entry.title,
            "link": entry.link,
            "description": description_text,
            "category": entry.get("category", "No category"),
            "guid": entry.get("guid", "No guid"),
            "published_date": entry.get("published", "No publish date"),
            "image_url": None
        }
        
        # Extract image URL if it exists
        if "enclosures" in entry and entry.enclosures:
            article["image_url"] = entry.enclosures[0].get("url", "No image available")
        
        articles.append(article)

    return articles

def remove_html_tags(html_text):
    # Parse the HTML content
    soup = BeautifulSoup(html_text, "html.parser")
    # Get only the text, without HTML tags
    clean_text = soup.get_text()
    return clean_text

def parse_detailed_rss(rss_url,sport):
    # Parse the RSS feed
    feed = feedparser.parse(rss_url)
    news_articles = []
    
    for entry in feed.entries:
        article = {
            "title": entry.title,
            "link": entry.link,
            "comments": entry.get("comments", "No comments link available"),
            "author": entry.get("dc:creator", "Unknown author"),
            "published_date": entry.get("published", "N/A"),
            "categories": [cat for cat in entry.get("category", [])],
            "description": remove_html_tags(entry.summary),
            "content": entry.get("content:encoded", "No content available"),
            "sport": sport
        }
        
        # Attempt to extract an image URL from description or content
        image_url = None
        if 'media_thumbnail' in entry:
            image_url = entry.media_thumbnail[0].get('url', None)
        elif 'media_content' in entry:
            image_url = entry.media_content[0].get('url', None)
        else:
            # Extract image URL from the HTML in the description if available
            img_match = re.search(r'src="([^"]+)"', entry.description)
            if img_match:
                image_url = img_match.group(1)
        
        article["image_url"] = image_url or "No image available"
        
        # Append article to the list
        news_articles.append(article)
    
    return news_articles

def fetch_news_from_rss(rss_urls, sport_type, limit=10):
    news_articles = []
    
    for rss_url in rss_urls:
        feed = feedparser.parse(rss_url)
        
        # Debugging: Print the feed structure to inspect its contents
        print(f"Feed URL: {rss_url}")
        print(f"Number of entries: {len(feed.entries)}")
        
        for entry in feed.entries:
            if len(news_articles) >= limit:
                break
            
            # Initialize article dictionary with available fields
            article = {
                "title": entry.title,
                "description": entry.summary,
                "link": entry.link,
                "sports": sport_type,
                "published_date": entry.get("published", "N/A")  # Get publication date if available
            }
            
            # Check for image URLs
            image_url = None
            if 'media_thumbnail' in entry:
                image_url = entry.media_thumbnail[0]['url']  # Check for media_thumbnail
            elif 'media_content' in entry:
                image_url = entry.media_content[0]['url']  # Check for media_content
            elif 'coverImages' in entry:
                image_url = entry.coverImages
            
            article["image_url"] = image_url or "No image available"
            
            # Add article if it doesn't already exist in the list
            if article not in news_articles:
                news_articles.append(article)

        if len(news_articles) >= limit:
            break

    return news_articles
# Sport-specific news fetching functions
def fetch_cricket_news():
    cricket_rss_urls = [
       "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
        "https://feeds.bbci.co.uk/sport/cricket/rss.xml"
    ]
    articles = fetch_news_from_rss(cricket_rss_urls, "cricket")
    for article in articles:
        save_article_to_firebase(article, "cricket")

def fetch_basket_news():
    basketball_rss_urls = [
        'https://basketball.realgm.com/rss/wiretap/0/0.xml'
    ]
    articles=fetch_news_from_rss(basketball_rss_urls, "basketball")
    for article in articles:
        save_article_to_firebase(article, "basketball")
def fetch_football_news():
    football_rss_urls = [
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "https://www.goal.com/feeds/en/news"
    ]
    articles = fetch_news_from_rss(football_rss_urls, "football")
    for article in articles:
        save_article_to_firebase(article, "football")

def fetch_tennis_news():
    tennis_rss_urls = [
    "https://feeds.bbci.co.uk/sport/tennis/rss.xml",
    ]
    articles = fetch_news_from_rss(tennis_rss_urls, "tennis")
    for article in articles:
        save_article_to_firebase(article, "tennis")
def fetch_badminton_news():
    badminton_rss_urls = "https://www.badmintonplanet.com/feed"
    
    articles = parse_detailed_rss(badminton_rss_urls, "badminton")
    for article in articles:
        save_article_to_firebase(article, "badminton")


def fetch_f1_news():
    f1_rss_url = "https://www.motorsport.com/rss/f1/news/"
    
    articles = parse_f1_rss(f1_rss_url)
    for article in articles:
        save_article_to_firebase(article, "f1")
#-------------------------------------------------------------------------------------------------------------------        



@app.route('/fetch_news', methods=['POST'])
def fetch_news():
    data = request.json
    preferences = data.get('preferences')  # Fetch preferences directly from request

    # Validate the preferences format if needed


    preferences = [preference.lower() for preference in preferences]
    # Fetch news based on preferences
    if "cricket" in preferences:
        fetch_cricket_news()
    if "football" in preferences:
        fetch_football_news()
    # Add other sports here:
    # if preferences.get("f1"):
    #     fetch_f1_news()
    # if preferences.get("hockey"):
    #     fetch_hockey_news()
    # ...

    return jsonify({"message": "Articles fetched and saved based on preferences"}), 200


@app.route('/get_news', methods=['GET'])
def get_news():
    fetch_cricket_news()
    fetch_football_news()
    fetch_tennis_news()
    fetch_badminton_news()
    fetch_f1_news()
    fetch_basket_news()
    return jsonify({"message": "Articles fetched and saved based on preferences"}), 200

@app.route('/recommend_articles', methods=['POST'])
def recommend_articles():
    try:
        data = request.get_json()
        preferences = data.get('preferences')

        if not preferences:
            return jsonify({"error": "No preferences provided"}), 400

        articles = []

            # Fetch articles for each preference
        for sport in preferences:
            articles_ref = db.collection('articles').where('sport', '==', sport.lower()).limit(10).get()
            for article in articles_ref:
                article_data = article.to_dict()  # Convert the document fields to a dictionary
                article_data['id'] = article.id
                articles += [article_data]
        unique_articles = {article['link']: article for article in articles}.values()
        recommended_articles = random.sample(list(unique_articles), min(len(unique_articles), 10))
        return jsonify({"recommended_articles": recommended_articles})

    except Exception as e:
        print(e)
        return jsonify({"error": str(e)}), 500
    

@app.route('/recommend_articles_get/<user_id>', methods=['GET'])
def recommend_articles_get(user_id):
    try:
        # Fetch user preferences from Firebase using user_id
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()

        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404

        # Get preferences from user document
        preferences = user_doc.to_dict().get('preferences', [])

        if not preferences:
            return jsonify({"error": "No preferences found for this user"}), 400

        articles = []

        # Fetch articles for each preference
        for sport in preferences:
            articles_ref = db.collection('articles')\
                .where('sport', '==', sport.lower())\
                .order_by('time', direction=firestore.Query.DESCENDING).get()

            for article in articles_ref:
                article_data = article.to_dict()  # Convert the document fields to a dictionary
                article_data['id'] = article.id
                articles.append(article_data)

        # Remove duplicate articles by 'link' and limit the number of recommended articles to 10
        unique_articles = {article['link']: article for article in articles}.values()
        recommended_articles = sorted(
            unique_articles, 
            key=lambda x: x['timestamp'], 
            reverse=True  # Ensure articles are sorted from latest to oldest
        )

        return jsonify({"recommended_articles": recommended_articles})

    except Exception as e:
        print(e)
        return jsonify({"error": str(e)}), 500


@app.route('/get_all_articles', methods=['GET'])
def get_all_articles():
    try:
        # Fetch all articles from Firestore
        articles_ref = db.collection('articles').order_by('time', direction=firestore.Query.DESCENDING).get()
        articles = []
        seen_titles = set()  # To keep track of titles we've already added

        for article in articles_ref:
            article_data = article.to_dict()  # Convert the document fields to a dictionary
            article_data['id'] = article.id
            title = article_data.get('title', '').strip()  # Get and normalize the title
            descriptions = article_data.get('description', "").strip()
            if title not in seen_titles and descriptions!="":  # Check if the title has not been added already
                articles.append(article_data)
                seen_titles.add(title)  # Mark this title as seen

        return jsonify({"articles": articles})

    except Exception as e:
        print(e)
        return jsonify({"error": str(e)}), 500

@app.route('/log_reading_behavior', methods=['POST'])
def log_reading_behavior():
    data = request.get_json()
    user_id = data['user_id']
        # user_mail = data['email']
        # user_password=data['password']
    article_id = data['article_id']
    sport = data['sport']
    team = data.get('team', [])
    player = data.get('player', [])
    tournament = data.get('tournament', [])

    # Add behavior log to Firestore
    behavior_ref = db.collection('user_behavior').add({
        'user_id': user_id,
        'article_id': article_id,
        'sport': sport,
        'team': team,
        'player': player,
        'tournament': tournament
    })

    # Get references to the user and article documents
    user_ref = db.collection('users').document(user_id)
    article_ref = db.collection('articles').document(article_id)

    # Update the article with the new player, team, and tournament information
    article_ref.update({
        'players': firestore.ArrayUnion(player),
        'teams': firestore.ArrayUnion(team),
        'tournaments': firestore.ArrayUnion(tournament),
    })

    # Check if the user document exists
    user_data = user_ref.get()

    if user_data.exists:
        # User document exists, so we can update it
        user_ref.update({f"sport_reads.{sport}": firestore.Increment(1)})

        # Update specific player, team, and tournament reads counts with sanitized names
        for p in player:
            sanitized_player = sanitize_name(p)
            user_ref.update({f"player_reads.{sanitized_player}": firestore.Increment(1)})

        for t in team:
            sanitized_team = sanitize_name(t)
            user_ref.update({f"team_reads.{sanitized_team}": firestore.Increment(1)})

        for tr in tournament:
            sanitized_tournament = sanitize_name(tr)
            user_ref.update({f"tournament_reads.{sanitized_tournament}": firestore.Increment(1)})

    else:
        # User document doesn't exist, so create it
        user_ref.set({
            'user_id': user_id,
            'sport_reads': {sport: 1},
            'player_reads': {sanitize_name(p): 1 for p in player} if player else {},
            'team_reads': {sanitize_name(t): 1 for t in team} if team else {},
            'tournament_reads': {sanitize_name(tr): 1 for tr in tournament} if tournament else {},
        })

    try:
        return jsonify({"success": True, "message": "Reading behavior logged successfully."}), 200
    except Exception as e:
        print(e)
        return jsonify({"success": False, "error": str(e)}), 400



'''
Recommendation code below
'''   
@app.route('/recommend_by_sport/<user_id>', methods=['GET'])
def recommend_by_sport(user_id):
    user_ref = db.collection('users').document(user_id)
    user_data=user_ref.get().to_dict()
    top_sports=get_top_filtered_list(user_data.get('sport_reads',{}),user_data.get('preferences',[]),5)
    news_list=[]
    for sport in top_sports:
        news=fetch_gemini_response(sport)
        news_list.append({sport: news})
    return jsonify({"news_list": news_list}), 200


@app.route('/recommend_by_players/<user_id>', methods=['GET'])
def recommend_by_players(user_id):
    user_ref = db.collection('users').document(user_id)
    user_data=user_ref.get().to_dict()
    top_players=get_top_filtered_list(user_data.get('player_reads',{}),user_data.get('players',[]),5)
    news_list=[]
    top_players=[desanitize_name(player) for player in top_players]
    for player in top_players:
        news=fetch_gemini_response(player)
        news_list.append({player: news})
    filtered_news_list = [item for item in news_list if list(item.values()) != [["No"]]]

    return jsonify({"news_list":filtered_news_list}), 200

@app.route('/recommend_by_teams/<user_id>', methods=['GET'])
def recommend_by_teams(user_id):
    user_ref = db.collection('users').document(user_id)
    user_data=user_ref.get().to_dict()
    top_teams=get_top_filtered_list(user_data.get('team_reads',{}),user_data.get('teams',[]),5)
    news_list=[]
    top_teams=[desanitize_name(team) for team in top_teams]
    # for team in top_teams:
    #     news=fetch_gemini_response(team)
    #     news_list.append({team: news})
    filtered_news_list = [item for item in news_list if list(item.values()) != [["No"]]]

    return jsonify({"news_list":filtered_news_list}), 200



@app.route('/recommend_by_tournaments/<user_id>', methods=['GET'])
def recommend_by_tournaments(user_id):
    user_ref = db.collection('users').document(user_id)
    user_data=user_ref.get().to_dict()
    top_tournaments=get_top_filtered_list(user_data.get('tournament_reads',{}),user_data.get('tournaments',[]),5)
    news_list=[]
    top_tournaments=[desanitize_name(tournament) for tournament in top_tournaments]
    for tournament in top_tournaments:
        news=fetch_gemini_response(tournament)
        news_list.append({tournament: news})
    filtered_news_list = [item for item in news_list if list(item.values()) != [["No"]]]

    return jsonify({"news_list":filtered_news_list}), 200

@app.route('/fetch_sport', methods=['POST'])
def fetch_sport():
    data = request.json
    sport_type = data.get("sport_type")  # Get sport type from the request data

    if sport_type == "cricket":
        fetch_cricket_news()
    elif sport_type == "football":
        fetch_football_news()
    elif sport_type == "tennis":
        fetch_tennis_news()
    elif sport_type == "badminton":
        fetch_badminton_news()
    elif sport_type == "f1":
        fetch_f1_news()
    elif sport_type == "basketball":
        fetch_basket_news()
    else:
        return jsonify({"error": "Invalid sport type"}), 400

    # After fetching the news, you can also retrieve and return the list of articles saved in Firestore
    articles_ref = db.collection('articles').where('sport', '==', sport_type).get()
    articles = []
    seen_titles=set()
    for article in articles_ref:
        article_data = article.to_dict()
        if article_data['title'] not in seen_titles and article_data['description']!="":
            articles.append(article_data)
            seen_titles.add(article_data['title'])

    return jsonify({"articles": articles}), 200

'''------------------------------------------------------------------------------------------------------'''

