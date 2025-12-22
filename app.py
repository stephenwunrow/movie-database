import os
import csv
import tempfile
from flask import Flask, request, render_template, redirect, url_for, flash, session
from flask_session import Session
import json
from werkzeug.utils import secure_filename
import requests
import xml.etree.ElementTree as ET
from gdrive_helper import download_tsv_from_gdrive, upload_tsv_to_gdrive
from google import genai
from google.genai import types
import string
import re
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "devkey")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "letmein")

# Temporary local TSV file - sync to Google Drive for persistence
TSV_FILE = 'Movies.tsv'

# Gemini API Setup (You will plug your key here)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

tmdb_key = os.getenv("tmdb_key")

def load_tsv():
    if not os.path.exists(TSV_FILE):
        return []
    with open(TSV_FILE, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))

def save_tsv(movies):
    fieldnames = ['ID', 'Title', 'Year', 'Runtime', 'Actors', 'Notes']
    with open(TSV_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
        writer.writeheader()
        for movie in movies:
            writer.writerow(movie)
    upload_tsv_to_gdrive()

def extract_titles_from_image(image_path):
    client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1'})

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    def try_model(model_name):
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg"
                ),
                "What are the titles of all the movies in this image? Return the titles only, with no other text, separated by line breaks."
            ]
        )
        return response

    try:
        response = try_model("gemini-2.5-flash")
        flash("Used model: gemini-2.5-flash", "info")
    except Exception as e:
        flash(f"gemini-2.5-flash failed with error: {e}. Trying gemini-2.5-flash-lite...", "warning")
        try:
            response = try_model("gemini-2.5-flash-lite")
            flash("Used model: gemini-2.5-flash-lite", "info")
        except Exception as e2:
            flash(f"Both models failed. Last error: {e2}", "error")
            return []

    titles_text = response.text.strip()
    titles = [line.strip() for line in titles_text.split('\n') if line.strip()]
    for title in titles:
        title = strip_punctuation(title.lower())
        title = re.sub('’', '\'', title)
        title = re.sub('  ', ' ', title)
    if titles:
        flash(f"Gemini extracted {len(titles)} title(s): " + ", ".join(titles), "info")
    else:
        flash("Gemini returned no titles from the image.", "warning")

    return titles

def strip_punctuation(text):
    return text.translate(str.maketrans('', '', string.punctuation))

def search_tmdb_movies(title):
    """Search TMDB for movies by title. Return a list of potential matches."""
    url = "https://api.themoviedb.org/3/search/movie"
    params = {
        "api_key": tmdb_key,
        "query": title,
        "include_adult": False  # optional
    }

    response = requests.get(url, params=params)
    if response.status_code != 200:
        print("Error:", response.status_code)
        return []

    data = response.json()
    results = data.get("results", [])
    if not results:
        return []

    title_clean = strip_punctuation(title.lower())
    matches = []

    for movie in results:
        movie_title = movie.get("title", "")
        movie_title_clean = strip_punctuation(movie_title.lower().strip())
        movie_title_clean = re.sub('  ', ' ', movie_title_clean)
        year = movie.get("release_date", "").split("-")[0]

        # Match exact title or partial match containing search term
        if title_clean in movie_title_clean:
            matches.append({
                "id": movie.get("id"),
                "title": movie_title,
                "release_date": year
            })

    return matches

def get_tmdb_movie_details(movie_id):
    """Fetch detailed info for a TMDb movie by ID"""
    
    # 1. Get basic movie details
    movie_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": tmdb_key}
    r = requests.get(movie_url, params=params)
    if r.status_code != 200:
        print("Error fetching movie details:", r.status_code)
        return None
    movie_data = r.json()
    
    # 2. Get movie credits to fetch actors
    credits_url = f"https://api.themoviedb.org/3/movie/{movie_id}/credits"
    r_credits = requests.get(credits_url, params=params)
    if r_credits.status_code != 200:
        print("Error fetching movie credits:", r_credits.status_code)
        return None
    credits_data = r_credits.json()
    
    # Extract cast names
    actors = [actor["name"] for actor in credits_data.get("cast", [])]
    actors_str = ", ".join(actors)  # All actors separated by commas
    
    # Build the result dictionary
    details = {
        "ID": movie_data.get("id"),
        "Title": movie_data.get("title", ""),
        "Year": movie_data.get("release_date", "").split("-")[0] if movie_data.get("release_date") else "",
        "Runtime": movie_data.get("runtime", ""),  # in minutes
        "Actors": actors_str,
        "Notes": ""  # leave blank for now
    }
    
    return details

def sort_movies(movies, sort_by):
    key_funcs = {
        'title': lambda g: g.get('Title', '').lower(),
        'year': lambda g: int(g.get('Year') or 0),
        'runtime': lambda g: g.get('Runtime') or 0,
        'actors': lambda g: g.get('Actors', '').lower(),
        'notes': lambda g: g.get('Notes', '').lower(),
    }

    if sort_by in key_funcs:
        return sorted(movies, key=key_funcs[sort_by])
    else:
        return movies  # return as-is for default order

# --- Session ---

app.config['SECRET_KEY'] = 'your-existing-secret-key'

app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = './flask_session'  # folder will be created
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True

Session(app)

# --- Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == SITE_PASSWORD:
            session['logged_in'] = True
            flash("Logged in successfully.", "success")
            return redirect(url_for('index'))
        else:
            flash("Incorrect password.", "error")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash("Logged out.", "info")
    return redirect(url_for('login'))

@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    sort_by = request.args.get('sort')

    if 'search_results' in session:
        movies = json.loads(session['search_results'])  # load filtered movies
        searched = True

    else:
        download_tsv_from_gdrive()
        movies = load_tsv()
        searched = False
    count = len(movies)

    if sort_by:
        if sort_by == 'title':
            movies.sort(key=lambda g: g['Title'].lower())
        elif sort_by == 'year':
            movies.sort(key=lambda g: float(g['Year']) if g['Year'] else 0)
        elif sort_by == 'runtime':
            movies.sort(key=lambda g: float(g['Runtime']) if g['Runtime'] else 0)
        elif sort_by == 'actors':
            movies.sort(key=lambda g: g['Actors'].lower() if g['Actors'] else '')
        elif sort_by == 'notes':
            movies.sort(key=lambda g: g['Notes'].lower() if g['Notes'] else '')

    return render_template('index.html', movies=movies, searched=searched, sort_by=sort_by, count=count)


@app.route('/upload-image', methods=['POST'])
def upload_image():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if 'image' not in request.files:
        flash("No image uploaded", "error")
        return redirect(url_for('index'))

    file = request.files['image']
    if file.filename == '':
        flash("No selected file", "error")
        return redirect(url_for('index'))

    filename = secure_filename(file.filename)
    temp_path = os.path.join(tempfile.gettempdir(), filename)
    file.save(temp_path)

    titles = extract_titles_from_image(temp_path)
    for title in titles:
        title = re.sub('’', '\'', title)
    if not titles:
        flash("No titles detected in image", "error")
        return redirect(url_for('index'))

    download_tsv_from_gdrive()
    movies = load_tsv()
    existing_titles = {g['Title'].lower() for g in movies}

    # Queue titles not already in the TSV
    session['pending_titles'] = [t for t in titles if t.lower() not in existing_titles]
    session['selected_movies'] = []
    session.modified = True

    if not session['pending_titles']:
        flash("All titles are already in the database.", "info")
        return redirect(url_for('index'))

    return redirect(url_for('process_next_title'))


@app.route('/process-next-title', methods=['GET', 'POST'])
def process_next_title():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    pending_titles = session.get('pending_titles', [])
    selected_movies = session.get('selected_movies', [])

    if not pending_titles:
        # When done, prepare 'pending_movies' for confirmation
        # Fetch details for all selected movies
        pending_movies = []
        for movie_id in selected_movies:
            details = get_tmdb_movie_details(movie_id)
            if details:
                pending_movies.append({'original_title': details['Title'], 'matches': [details]})
        session['pending_movies'] = pending_movies

        # Clear pending_titles and selected_movies
        session.pop('pending_titles', None)
        # session.pop('selected_movies', None)
        session.modified = True

        return redirect(url_for('confirm_add_all'))

    current_title = pending_titles[0]

    if request.method == 'POST':
        action = request.form.get('action')
        selected_movie_id = request.form.get('selected_movie_id')

        if action == 'reject':
            # Skip this title entirely
            pending_titles.pop(0)
            session['pending_titles'] = pending_titles
            session['selected_movies'] = selected_movies
            session.modified = True

            if pending_titles:
                return redirect(url_for('process_next_title'))
            else:
                # Same end-of-queue behavior as before
                pending_movies = []
                for movie_id in selected_movies:
                    details = get_tmdb_movie_details(movie_id)
                    if details:
                        pending_movies.append({
                            'original_title': details['Title'],
                            'matches': [details]
                        })
                session['pending_movies'] = pending_movies
                session.pop('pending_titles', None)
                session.modified = True

                return redirect(url_for('confirm_add_all'))
        
        if not selected_movie_id:
            flash("Please select a movie to add.", "error")
            # We'll re-render page below

        else:
            selected_movies.append(selected_movie_id)
            pending_titles.pop(0)
            session['pending_titles'] = pending_titles
            session['selected_movies'] = selected_movies
            session.modified = True

            if pending_titles:
                return redirect(url_for('process_next_title'))
            else:
                # Prepare 'pending_movies' for confirmation immediately
                pending_movies = []
                for movie_id in selected_movies:
                    details = get_tmdb_movie_details(movie_id)
                    if details:
                        pending_movies.append({'original_title': details['Title'], 'matches': [details]})
                session['pending_movies'] = pending_movies

                # Clear pending_titles and selected_movies since done
                session.pop('pending_titles', None)
                # session.pop('selected_movies', None)
                session.modified = True

                return redirect(url_for('confirm_add_all'))

    # GET request or POST with no selection: search matches
    matches = search_tmdb_movies(current_title)

    if not matches:
        flash(f"Could not find '{current_title}' on TMDb.", "warning")
        # skip this title, remove from pending and continue
        pending_titles.pop(0)
        session['pending_titles'] = pending_titles
        session.modified = True
        return redirect(url_for('process_next_title'))

    if len(matches) == 1:
        # Automatically select single match
        selected_movies.append(matches[0]['id'])
        pending_titles.pop(0)
        session['pending_titles'] = pending_titles
        session['selected_movies'] = selected_movies
        session.modified = True
        return redirect(url_for('process_next_title'))

    # Multiple matches: render selection page
    return render_template('choose_many_movies.html', matches=matches, original_title=current_title)

@app.route('/confirm-add-all', methods=['GET', 'POST'])
def confirm_add_all():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    selected_movie_ids = session.get('selected_movies', [])
    if not selected_movie_ids:
        flash("No movies selected to add.", "info")
        return redirect(url_for('index'))

    if request.method == 'POST':
        movies = load_tsv()
        existing_titles = {g['Title'].lower() for g in movies}
        newly_added = 0
        for movie_id in selected_movie_ids:
            details = get_tmdb_movie_details(movie_id)
            if details and details['Title'].lower() not in existing_titles:
                movies.insert(0, details)
                newly_added += 1
                existing_titles.add(details['Title'].lower())

        save_tsv(movies)
        flash(f"Added {newly_added} new movies to the database.", "success")

        # Clear session data
        session.pop('pending_titles', None)
        session.pop('selected_movies', None)
        session.modified = True

        return redirect(url_for('index'))

    # GET: show all selected movies details for final confirmation
    detailed_movies = []
    for movie_id in selected_movie_ids:
        details = get_tmdb_movie_details(movie_id)
        if details:
            detailed_movies.append(details)

    return render_template('confirm_add_all.html', movies=detailed_movies)

@app.route('/add-by-title', methods=['POST'])
def add_by_title():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    session.pop('pending_titles', None)
    session.pop('selected_movies', None)
    session.pop('pending_movies', None)

    title = request.form.get('title')
    title = re.sub('(’|‘)', '\'', title)
    if not title:
        flash("Please enter a movie title", "error")
        return redirect(url_for('index'))

    movies = load_tsv()
    if any(g['Title'].lower() == title.lower() for g in movies):
        flash(f"{title} is already in the database.", "info")
        return redirect(url_for('index'))

    # Search BGG for multiple matches
    title = strip_punctuation(title.lower())
    title = title.strip()
    matches = search_tmdb_movies(title)
    if not matches:
        flash(f"No matches found for '{title}' on TMDb.", "error")
        return redirect(url_for('index'))

    # If only one match, add it directly
    if len(matches) == 1:
        return redirect(url_for('confirm_add', selected_movie_id=matches[0]['id']))

    # Otherwise, show selection template
    return render_template('choose_movie.html', matches=matches, original_title=title)

@app.route('/confirm-add', methods=['GET', 'POST'])
def confirm_add():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        selected_movie_id = request.form.get('selected_movie_id')

        if not selected_movie_id:
            flash("Please select a movie to add.", "error")
            return redirect(url_for('index'))

        details = get_tmdb_movie_details(selected_movie_id)
        if not details:
            flash("Could not retrieve movie details.", "error")
            return redirect(url_for('index'))

        movies = load_tsv()
        if any(g['Title'].lower() == details['Title'].lower() for g in movies):
            flash(f"'{details['Title']}' is already in the database.", "info")
        else:
            movies.insert(0, details)
            save_tsv(movies)
            flash(f"Added '{details['Title']}' to the database.", "success")

        return redirect(url_for('index'))

    # GET request: maybe redirected here with ?selected_movie_id=
    selected_movie_id = request.args.get('selected_movie_id')
    if selected_movie_id:
        details = get_tmdb_movie_details(selected_movie_id)
        if not details:
            flash("Could not retrieve movie details.", "error")
            return redirect(url_for('index'))

        return render_template('confirm_add.html', original_title=details)

    # Default fallback
    flash("Nothing to confirm.", "warning")
    return redirect(url_for('index'))



@app.route('/search', methods=['GET', 'POST'])
def search():
    download_tsv_from_gdrive()
    movies = load_tsv()

    if request.method == 'POST':
        # Get sort param from query or default None
        sort_by = request.args.get('sort') or None

        # Get all search fields, default empty strings
        title = request.form.get('title', '').lower().strip()
        year = request.form.get('year', '').lower().strip()
        runtime = request.form.get('runtime', '').strip()
        actors = request.form.get('actors', '').lower().strip()
        notes = request.form.get('notes', '').lower().strip()

        def matches(movie):
            if title and title not in movie['Title'].lower():
                return False
            if year and year not in movie['Year'].lower():
                return False
            if actors and actors not in movie.get('Actors', '').lower():
                return False
            if notes and notes not in movie.get('Notes', '').lower():
                return False
            if runtime:
                try:
                    if movie['Runtime']:
                        w = float(movie['Runtime'])
                        target = float(runtime)
                        if not (target - 10 <= w <= target + 10):
                            return False
                except ValueError:
                    return False
            return True

        filtered = [g for g in movies if matches(g)]

        # Sort filtered if sort_by present
        if sort_by:
            filtered = sort_movies(filtered, sort_by)

        # Store filtered results in session for consistency
        session['search_results'] = json.dumps(filtered)

        count = len(filtered)

        return render_template('index.html', movies=filtered, sort_by=sort_by, searched=True, count=count)

    # GET request shows all movies
    sort_by = request.args.get('sort')
    if 'search_results' in session:
        movies = json.loads(session['search_results'])
        searched = True
    else:
        searched = False

    if sort_by:
        movies = sort_movies(movies, sort_by)
    
    count = len(movies)

    return render_template('index.html', movies=movies, sort_by=sort_by, searched=searched, count=count)

@app.route('/edit/<title>', methods=['GET', 'POST'])
def edit(title):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    download_tsv_from_gdrive()
    movies = load_tsv()
    movie = next((g for g in movies if g['Title'].lower() == title.lower()), None)
    if movie is None:
        flash("movie not found", "error")
        return redirect(url_for('index'))

    if request.method == 'POST':
        # Update movie info from form fields
        movie['Title'] = request.form.get('title', movie['Title'])
        movie['Year'] = request.form.get('year', movie['Year'])
        movie['Runtime'] = request.form.get('runtime', movie['Runtime'])
        movie['Actors'] = request.form.get('actors', movie['Actors'])
        movie['Notes'] = request.form.get('notes', movie['Notes'])

        save_tsv(movies)
        flash("movie updated successfully", "success")
        return redirect(url_for('index'))

    return render_template('edit.html', movie=movie)

@app.route('/delete/<movie_id>', methods=['POST'])
def delete_movie(movie_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    movies = load_tsv()
    updated_movies = [g for g in movies if str(g.get('ID')) != str(movie_id)]

    if len(updated_movies) == len(movies):
        flash("movie not found.", "error")
    else:
        save_tsv(updated_movies)
        flash("movie deleted successfully.", "success")

    return redirect(url_for('index'))

@app.route('/clear')
def clear():
    session.pop('search_results', None)
    return redirect(url_for('index'))

@app.route('/search-by-image', methods=['POST'])
def search_by_image():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    if 'image' not in request.files:
        flash("No image uploaded", "error")
        return redirect(url_for('index'))
    file = request.files['image']
    if file.filename == '':
        flash("No selected file", "error")
        return redirect(url_for('index'))
    filename = secure_filename(file.filename)
    temp_path = os.path.join(tempfile.gettempdir(), filename)
    file.save(temp_path)

    titles = extract_titles_from_image(temp_path)
    if not titles:
        flash("No titles detected in image", "error")
        return redirect(url_for('index'))

    download_tsv_from_gdrive()
    movies = load_tsv()
    results = []
    lower_movies = {g['Title'].lower(): g for g in movies}
    for title in titles:
        g = lower_movies.get(title.lower())
        if g:
            results.append(g)
        if not g:
            flash(f"{title} not found")

    if not results:
        flash("No matching movies found for detected titles", "info")

    return render_template('index.html', movies=results, searched=True)

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)