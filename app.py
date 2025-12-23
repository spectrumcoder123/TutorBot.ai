from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_wtf import FlaskForm
from langchain.memory import ConversationBufferMemory 
from wtforms import StringField, PasswordField, SubmitField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError
import bcrypt
from db_shim import MySQL
import os
from tutor import SubjectTutor
from markdown import markdown as md
from dotenv import load_dotenv
import re
import json
import time
import pandas as pd
import plotly.express as px
import plotly.io as pio # Added for Plotly template setting

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# MySQL connection
app.config['MYSQL_HOST'] = os.getenv('MYSQL_HOST')
app.config['MYSQL_USER'] = os.getenv('MYSQL_USER')
app.config['MYSQL_PASSWORD'] = os.getenv('MYSQL_PASSWORD')
app.config['MYSQL_DB'] = os.getenv('MYSQL_DB', 'tutorbot')
app.config['MYSQL_PORT'] = int(os.getenv('MYSQL_PORT', 4000))
app.config['MYSQL_SSL_CA'] = os.getenv('MYSQL_SSL_CA', None)
# TiDB requires SSL, so we ensure it's enabled if a CA is provided or if we are in production
# For now, we rely on the shim to handle it based on the config.
mysql = MySQL(app)

from markupsafe import Markup

# Add markdown filter to Jinja2
def markdown_filter(text):
    return Markup(md(text))

app.jinja_env.filters['markdown'] = markdown_filter
pio.templates.default = "plotly_white" # Setting a default Plotly template for clean graphs

# Define subjects for the notes and progress forms
SUBJECTS = ['dsa', 'ml', 'dbms', 'os', 'webdev', 'networks']

# --- WTForms (No conflict) ---

class RegisterForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Register')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')

class NotesForm(FlaskForm):
    subject = SelectField('Subject', choices=[(s, s.capitalize()) for s in SUBJECTS], validators=[DataRequired()])
    note_content = TextAreaField('Note Content', validators=[DataRequired(), Length(max=10000, message="Note content must not exceed 10,000 characters")])
    submit = SubmitField('Save Note')

class SettingsForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_username = StringField('New Username', validators=[Length(min=2, max=50)])
    new_password = PasswordField('New Password', validators=[Length(min=6, message="Password must be at least 6 characters")])
    confirm_password = PasswordField('Confirm New Password', validators=[EqualTo('new_password', message="Passwords must match")])
    submit = SubmitField('Save Changes')

    def validate_current_password(self, field):
        if 'user_id' in session:
            cursor = mysql.connection.cursor()
            cursor.execute('SELECT password FROM users WHERE id = %s', (session['user_id'],))
            user = cursor.fetchone()
            cursor.close()
            if user and not bcrypt.checkpw(field.data.encode('utf-8'), user[0].encode('utf-8')):
                raise ValidationError('Current password is incorrect.')

# Define topics per subject for quiz context
TOPICS = {
    'dsa': ['Arrays', 'Linked Lists', 'Stacks', 'Queues', 'Trees', 'Graphs', 'Sorting', 'Searching'],
    'ml': ['Supervised Learning', 'Unsupervised Learning', 'Neural Networks', 'Regression', 'Classification'],
    'dbms': ['ER Model', 'Normalization', 'Transactions', 'Indexing', 'SQL'],
    'os': ['Processes', 'Threads', 'Memory Management', 'Scheduling', 'File Systems'],
    'webdev': ['HTML', 'CSS', 'JavaScript', 'React', 'Node.js'],
    'networks': ['OSI Model', 'TCP/IP', 'Routing', 'Subnetting', 'HTTP']
}

# --- Utility Functions (From your original code) ---

def get_subjects_with_topics():
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT sub_id, sub_name FROM subjects")
    subjects = cursor.fetchall()
    data = []
    for subj in subjects:
        cursor.execute("SELECT topic_id, topic_name FROM topics WHERE sub_id=%s", (subj[0],))
        topics = cursor.fetchall()
        data.append({"sub_id": subj[0], "sub_name": subj[1], "topics": topics})
    
    print(data)
    cursor.close()
    return data

def get_user_progress(user_id):
    subjects_data = get_subjects_with_topics()

    cursor = mysql.connection.cursor()
    cursor.execute('SELECT topic_id FROM user_progress WHERE user_id = %s', (user_id,))
    completed_rows = cursor.fetchall()
    cursor.close()
    
    completed_topic_ids = set(row[0] for row in completed_rows)

    subject_progress = {}
    for subject in subjects_data:
        total_topics = len(subject.get("topics", []))
        completed_topics = sum(1 for topic in subject["topics"] if topic[0] in completed_topic_ids)
        progress_pct = int(round((completed_topics / total_topics) * 100)) if total_topics > 0 else 0
        subject_progress[subject["sub_name"]] = progress_pct

    return subject_progress

# --- Routes (Merged Logic) ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/home')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    else:
        return redirect(url_for('index'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data
        password = form.password.data
        
        cursor = mysql.connection.cursor()
        cursor.execute('SELECT * FROM users WHERE email = %s', (email,))
        user = cursor.fetchone()
        cursor.close()
        
        if user and bcrypt.checkpw(password.encode('utf-8'), user[3].encode('utf-8')):
            session['user_id'] = user[0]
            # Fetch user name for session (useful for dashboard)
            session['user_name'] = user[1] 
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')
    
    return render_template('login.html', form=form)

@app.route('/register', methods=['GET', 'POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        name = form.name.data
        email = form.email.data
        password = form.password.data
        
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        cursor = mysql.connection.cursor()
        cursor.execute('INSERT INTO users (name, email, password) VALUES (%s, %s, %s)', (name, email, hashed_password))
        mysql.connection.commit()
        cursor.close()
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html', form=form)

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash("Please log in first", "error")
        return redirect(url_for('login'))

    user_id = session['user_id']

    cursor = mysql.connection.cursor()

    cursor.execute("SELECT name FROM users WHERE id = %s", (user_id,))
    user_row = cursor.fetchone()
    user_name = user_row[0] if user_row else "User"

    cursor.execute("SELECT COUNT(DISTINCT quiz_no) FROM quiz_attempts WHERE user_id = %s", (user_id,))
    quizzes_taken = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM user_notes WHERE user_id = %s", (user_id,))
    notes_saved = cursor.fetchone()[0]

    hours_learned = 0 # Placeholder for hours learned

    cursor.close()

    subject_progress = get_user_progress(user_id) # Uses the utility function from your original code

    # Renders the main dashboard page (using your original template name 'home.html')
    return render_template(
        'home.html',
        user_name=user_name,
        hours_learned=hours_learned,
        quizzes_taken=quizzes_taken,
        notes_saved=notes_saved,
        subject_progress=subject_progress
    )

# CONFLICT RESOLUTION: This route now uses the most comprehensive analytics logic
# from your friend's code to populate the user_dashboard.html template.
@app.route('/quiz_analytics')
def quiz_analytics():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    user_id = session['user_id']

    cursor = mysql.connection.cursor()
    # Note: Friend's code fetched ALL quiz attempts, modified here to only fetch for logged-in user
    cursor.execute('SELECT * FROM quiz_attempts WHERE user_id = %s', (user_id,)) 

    rows = cursor.fetchall()
    col_names = [i[0] for i in cursor.description]
    cursor.close()
    
    if not rows:
        flash("No quiz data to display analytics. Take some quizzes first!", "info")
        # Return a page without plots if no data exists
        return render_template('user_dashboard.html', 
                               sub_bar=None, sub_line=None, 
                               sub_dsa=None, sub_ml=None, sub_dbms=None, 
                               sub_os=None, sub_webdev=None, sub_networks=None)

    # Create DataFrame
    df = pd.DataFrame(rows, columns=col_names)

    # Convert correct_option (1-4) and user_option (1-4, 0 if not answered) to boolean for correctness
    df['marks'] = df['correct_option'] == df['user_option']
    df['marks'] = df['marks'].astype(int)

    # --- 1. Subject-wise Bargraph (Accuracy per Subject) ---
    accuracy_df = df.groupby('subject').agg(
        total_questions=('marks', 'count'),
        correct_answers=('marks', 'sum')
    ).reset_index()

    accuracy_df['accuracy'] = (accuracy_df['correct_answers'] / accuracy_df['total_questions']) * 100
    accuracy_df['accuracy'] = accuracy_df['accuracy'].round(2)

    bar_fig = px.bar(
        accuracy_df,
        x='subject',
        y='accuracy',
        color='subject',
        title='Accuracy per Subject (%)',
        labels={'accuracy': 'Accuracy (%)', 'subject': 'Subject'},
        text='accuracy'
    )

    bar_fig.update_traces(textposition='outside')
    bar_fig.update_layout(yaxis_range=[0, 100], height=600, width=1000)
    sub_bar = bar_fig.to_html(full_html=False)
    
    # --- 2. Subject-wise Line Graph (Performance Over Time) ---
    df['created_at'] = pd.to_datetime(df['created_at'])
    df['date'] = df['created_at'].dt.date

    accuracy_df_2 = df.groupby(['subject', 'date']).agg(
        total_attempts=('marks', 'count'),
        correct_answers=('marks', 'sum')
    ).reset_index()

    accuracy_df_2['accuracy_percent'] = (accuracy_df_2['correct_answers'] / accuracy_df_2['total_attempts']) * 100
    accuracy_df_2['accuracy_percent'] = accuracy_df_2['accuracy_percent'].round(2)

    line_fig = px.line(
        accuracy_df_2,
        x='date',
        y='accuracy_percent',
        color='subject',
        markers = True,
        title='Performance Over Time by Subject',
        labels={
            'date': 'Date',
            'accuracy_percent': 'Marks (%)',
            'subject': 'Subject'
        }
    )

    line_fig.update_layout(
        yaxis_range=[0, 100], 
        xaxis_title='Date',
        yaxis_title='Marks (%)',
        xaxis_autorange=True,
        yaxis_autorange=True,
        autosize=True,
        height=600,
        width=1000,
        margin=dict(l=40, r=40, t=80, b=40)
    )

    sub_line = line_fig.to_html(full_html=False)

    # --- 3. Topic-wise Bargraphs (Accuracy per Topic per Subject) ---
    accuracy_df_3 = df.groupby(['subject', 'topic']).agg(
        total_questions=('marks', 'count'),
        correct_answers=('marks', 'sum')
    ).reset_index()

    accuracy_df_3['accuracy_percent'] = (accuracy_df_3['correct_answers'] / accuracy_df_3['total_questions']) * 100
    accuracy_df_3['accuracy_percent'] = accuracy_df_3['accuracy_percent'].round(2)

    # Function to generate plot for a specific subject
    def generate_topic_plot(subject_name, df_data):
        subject_df = df_data[df_data['subject'] == subject_name]
        if subject_df.empty:
            return None
        
        fig = px.bar(
            subject_df,
            x='topic',
            y='accuracy_percent',
            color='topic',
            title=f'{subject_name.upper()} Accuracy per Topic (%)',
            labels={'accuracy_percent': 'Accuracy (%)', 'topic': 'Topic'},
            text='accuracy_percent'
        )
        fig.update_traces(textposition='outside')
        fig.update_layout(yaxis_range=[0, 100], height=600, width=1000)
        return fig.to_html(full_html=False)

    sub_dsa = generate_topic_plot('dsa', accuracy_df_3)
    sub_ml = generate_topic_plot('ml', accuracy_df_3)
    sub_dbms = generate_topic_plot('dbms', accuracy_df_3)
    sub_os = generate_topic_plot('os', accuracy_df_3)
    sub_webdev = generate_topic_plot('webdev', accuracy_df_3)
    sub_networks = generate_topic_plot('networks', accuracy_df_3)
    
    # Renders the analytics page (using your original template name 'user_dashboard.html')
    return render_template(
        'user_dashboard.html', 
        sub_bar = sub_bar, 
        sub_line = sub_line, 
        sub_dsa = sub_dsa, 
        sub_ml = sub_ml, 
        sub_dbms = sub_dbms, 
        sub_os = sub_os, 
        sub_webdev = sub_webdev, 
        sub_networks = sub_networks
    )

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/session')
def session_page():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    return render_template('session.html')


@app.route('/progress_tracker')
def progress_tracker():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    subjects_data = get_subjects_with_topics()
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT topic_id FROM user_progress WHERE user_id = %s', (session['user_id'],))
    completed_rows = cursor.fetchall()
    cursor.close()
    completed_topic_ids = set(row[0] for row in completed_rows)
    
    for subject in subjects_data:
        total_topics = len(subject["topics"]) if subject.get("topics") else 0
        completed_topics = 0
        if total_topics > 0:
            for topic in subject["topics"]:
                if topic[0] in completed_topic_ids:
                    completed_topics += 1
            progress_pct = int(round((completed_topics / total_topics) * 100))
        else:
            progress_pct = 0
        subject["total_topics"] = total_topics
        subject["completed_topics"] = completed_topics
        subject["progress_pct"] = progress_pct

    return render_template("progress_tracker.html", subjects=subjects_data, completed_topic_ids=completed_topic_ids)


@app.route('/api/progress', methods=['POST'])
def update_user_progress():
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        payload = request.get_json(silent=True) or {}
        topic_id = payload.get('topic_id')
        completed = bool(payload.get('completed'))

        if topic_id is None:
            return jsonify({"success": False, "error": "topic_id is required"}), 400

        cursor = mysql.connection.cursor()

        if completed:
            cursor.execute('INSERT IGNORE INTO user_progress (user_id, topic_id) VALUES (%s, %s)', (session['user_id'], topic_id))
        else:
            cursor.execute('DELETE FROM user_progress WHERE user_id = %s AND topic_id = %s', (session['user_id'], topic_id))

        mysql.connection.commit()
        cursor.close()

        return jsonify({"success": True})
    except Exception as e:
        try:
            cursor.close()
        except Exception:
            pass
        return jsonify({"success": False, "error": str(e)}), 500
    
@app.route('/tutors/<subject>_tutor', methods=['GET', 'POST'])
def tutor_page(subject):
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))

    form = NotesForm()
    existing_note = None
    tutor_response = None
    tutor = SubjectTutor()

    # Pre-select the subject in the form
    form.subject.data = subject

    if form.validate_on_submit():
        note_content = form.note_content.data
        user_id = session['user_id']
        
        try:
            cursor = mysql.connection.cursor()
            cursor.execute(
                'INSERT INTO user_notes (user_id, subject, note_content) VALUES (%s, %s, %s)',
                (user_id, form.subject.data, note_content)
            )
            mysql.connection.commit()
            
            # The original code's url_for call for update_user_progress was incorrect (not callable from here)
            # Removed the incorrect update_user_progress call inside tutor_page
            
            cursor.close()
            flash('Note saved successfully!', 'success')
        except Exception as e:
            flash(f'Error saving note: {str(e)}', 'error')
            # Ensure cursor is closed on error
            try:
                 cursor.close()
            except Exception:
                pass
    
    elif request.method == 'POST' and 'query' in request.form:
        query = request.form['query']
        tutor_response = tutor.generate_response(subject, query)
        
        print(f"Tutor response: {tutor_response}")

    else:
        try:
            cursor = mysql.connection.cursor()
            cursor.execute(
                'SELECT note_content FROM user_notes WHERE user_id = %s AND subject = %s ORDER BY created_at DESC LIMIT 1',
                (session['user_id'], subject)
            )
            existing_note = cursor.fetchone()
            cursor.close()
        except Exception as e:
            flash(f'Error fetching note: {str(e)}', 'error')
            # Ensure cursor is closed on error
            try:
                 cursor.close()
            except Exception:
                pass

    subject_templates = {
        'dsa': 'tutors/dsa_tutor.html',
        'ml': 'tutors/ml_tutor.html',
        'dbms': 'tutors/dbms_tutor.html',
        'os': 'tutors/os_tutor.html',
        'webdev': 'tutors/webdev_tutor.html',
        'networks': 'tutors/networks_tutor.html'
    }
    
    template_name = subject_templates.get(subject)
    if template_name:
        return render_template(template_name, form=form, existing_note=existing_note[0] if existing_note else "", 
                              tutor_response=tutor_response)
    else:
        flash('Subject not found.', 'error')
        return redirect(url_for('session_page'))

@app.route('/review_notes')
def review_notes():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    cursor = mysql.connection.cursor()
    query = "SELECT id, subject, note_content, created_at FROM user_notes WHERE user_id = %s"
    params = [session['user_id']]
    subject = request.args.get('subject', '')
    date_str = request.args.get('date', '')

    if subject:
        query += " AND subject = %s"
        params.append(subject)
    if date_str:
        query += " AND DATE(created_at) = %s"
        params.append(date_str)

    print(f"Query: {query}, Params: {params}")
    cursor.execute(query, params)
    notes = cursor.fetchall()
    cursor.close()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'notes': [{'id': note[0], 'subject': note[1], 'note_content': note[2], 'created_at': note[3].isoformat()} for note in notes]})

    return render_template('review_notes.html', notes=notes, user_name=session.get('user_name'))

@app.route('/notes', methods=['GET', 'POST'])
def notes():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))

    form = NotesForm()
    notes = []

    if form.validate_on_submit():
        note_content = form.note_content.data
        subject = form.subject.data
        user_id = session['user_id']

        try:
            cursor = mysql.connection.cursor()
            cursor.execute(
                'INSERT INTO user_notes (user_id, subject, note_content) VALUES (%s, %s, %s)',
                (user_id, subject, note_content)
            )
            mysql.connection.commit()
            cursor.close()
            flash('Note saved successfully!', 'success')
        except Exception as e:
            flash(f'Error saving note: {str(e)}', 'error')
            try:
                 cursor.close()
            except Exception:
                pass

    try:
        cursor = mysql.connection.cursor()
        cursor.execute(
            'SELECT id, subject, note_content, created_at FROM user_notes WHERE user_id = %s ORDER BY created_at DESC',
            (session['user_id'],)
        )
        notes = cursor.fetchall()
        cursor.close()
    except Exception as e:
        flash(f'Error fetching notes: {str(e)}', 'error')
        try:
            cursor.close()
        except Exception:
            pass

    return render_template('notes.html', form=form, notes=notes)


@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    tutor = SubjectTutor()
    quiz_data = session.get('quiz_data', None)

    if request.method == 'POST':
        subject = request.form.get('subject')
        level = request.form.get('level')
        topic = request.form.get('topic')
        num_questions = int(request.form.get('num_questions', 5))

        session['quiz_context'] = {'subject': subject, 'topic': topic}

        query = f"Generate {num_questions} multiple-choice questions on {subject} for {level} level, focusing on {topic}.Each question should have a question text, 4 options (a, b, c, d), one correct answer (index 0-3), and a brief explanation of the correct answer. Return the response as a JSON object with a 'questions' array, where each question is an object with 'question', 'options' (array of 4), 'correct_answer' (index 0-3), and 'explanation' (string). IF REQUIRED QUESTIONS ARE 20: then for sure generate 20 questions without fail in the JSON format"
        quiz_raw = tutor.generate_response(subject, query)
        
        try:
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', quiz_raw, re.DOTALL)
            if json_match:
                quiz_raw = json_match.group(1).strip()
            elif quiz_raw.strip().startswith('{'):
                quiz_raw = quiz_raw.strip()
            else:
                raise ValueError("No valid JSON structure detected in response")
            
            quiz_data = json.loads(quiz_raw)
            if not isinstance(quiz_data, dict) or 'questions' not in quiz_data:
                raise ValueError("Invalid quiz data structure: Missing 'questions' key or not a dictionary")
            
            session['quiz_data'] = quiz_data
            flash(f'Quiz generated for {subject} - {topic} ({level}, {num_questions} questions)', 'success')
        except (json.JSONDecodeError, ValueError) as e:
            flash(f'Error parsing quiz data: {str(e)}. Please try again with different parameters.', 'error')
            quiz_data = None
            print(f"JSON parsing failed. Error: {str(e)}, Raw response: {quiz_raw}")

    return render_template('quiz.html', quiz_data=quiz_data, topics=TOPICS)

@app.route('/submit_quiz', methods=['POST'])
def submit_quiz():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    quiz_data = session.get('quiz_data')
    quiz_context = session.get('quiz_context', {'subject': '', 'topic': ''})
    print(f"Quiz context during submission: {quiz_context}")
    if not quiz_data:
        flash('No quiz data available. Please generate a quiz first.', 'error')
        return redirect(url_for('quiz'))

    score = 0
    total_questions = len(quiz_data['questions'])
    user_answers = {}
    
    print(f"Received form data: {dict(request.form)}")
    
    cursor = mysql.connection.cursor()
    quiz_timestamp = int(round(time.time()))
    
    for i in range(total_questions):
        user_answer_key = request.form.get(f'answer_{i}')
        correct_answer_index = quiz_data['questions'][i]['correct_answer']
        
        user_index = -1
        if user_answer_key and user_answer_key.strip().lower() in ['a', 'b', 'c', 'd']:
            user_index = ord(user_answer_key.strip().lower()) - ord('a')
            if user_index == correct_answer_index:
                score += 1

        user_answers[i] = user_answer_key

        correct_option_db = correct_answer_index + 1
        # Set user_option_db to 0 if no answer, otherwise 1-4
        user_option_db = user_index + 1 if user_index != -1 else 0 

        try:
            cursor.execute(
                """
                INSERT INTO quiz_attempts (user_id, quiz_no, question, option1, option2, option3, option4, correct_option, user_option, subject, topic)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session['user_id'],
                    quiz_timestamp,
                    quiz_data['questions'][i]['question'],
                    quiz_data['questions'][i]['options'][0],
                    quiz_data['questions'][i]['options'][1],
                    quiz_data['questions'][i]['options'][2],
                    quiz_data['questions'][i]['options'][3],
                    correct_option_db,
                    user_option_db,
                    quiz_context.get('subject', ''),
                    quiz_context.get('topic', '')
                )
            )
        except Exception as e:
            print(f"Error logging quiz attempt {i}: {e}")
    
    mysql.connection.commit()
    
    percentage = (score / total_questions) * 100 if total_questions > 0 else 0
    
    # Logic for updating user progress (from your original code)
    # The topic must be the ID, which is stored in the quiz_context from the quiz form/request.
    # This relies on your HTML/JS passing the actual topic_id from the dropdown.
    if quiz_context.get('topic'):
        # Attempt to insert the topic as completed (if not already there)
        # Note: This assumes the 'topic' value in quiz_context is the topic_id from the database
        cursor.execute(
            'INSERT IGNORE INTO user_progress (user_id, topic_id) VALUES (%s, %s)',
            (session['user_id'], quiz_context.get('topic'))
        )
    cursor.close()
    
    session['quiz_results'] = {
        'score': score,
        'total_questions': total_questions,
        'percentage': percentage,
        'user_answers': user_answers,
        'quiz_data': quiz_data
    }
    
    flash(f'Quiz submitted! Your score: {score}/{total_questions} ({percentage:.1f}%)', 'success')
    return redirect(url_for('review_quiz')) # Changed redirect to review_quiz for better UX

@app.route('/review_quiz')
def review_quiz():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    quiz_results = session.get('quiz_results')
    if not quiz_results:
        flash('No quiz results available. Please take a quiz first.', 'error')
        return redirect(url_for('quiz'))
    
    score = quiz_results['score']
    total_questions = quiz_results['total_questions']
    percentage = quiz_results['percentage']
    user_answers = quiz_results['user_answers']
    quiz_data = quiz_results['quiz_data']
    
    return render_template('review_quiz.html', score=score, total_questions=total_questions, 
                          percentage=percentage, user_answers=user_answers, quiz_data=quiz_data)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    form = SettingsForm()
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT name FROM users WHERE id = %s', (session['user_id'],))
    current_username = cursor.fetchone()[0]
    cursor.close()

    if form.validate_on_submit():
        cursor = mysql.connection.cursor()
        cursor.execute('SELECT password FROM users WHERE id = %s', (session['user_id'],))
        user = cursor.fetchone()
        
        if user and bcrypt.checkpw(form.current_password.data.encode('utf-8'), user[0].encode('utf-8')):
            new_username = form.new_username.data.strip() if form.new_username.data else None
            new_password = form.new_password.data.strip() if form.new_password.data else None
            
            if new_username or new_password:
                update_query = 'UPDATE users SET '
                update_params = []
                if new_username:
                    update_query += 'name = %s'
                    update_params.append(new_username)
                if new_password:
                    if new_username:
                        update_query += ', password = %s'
                    else:
                        update_query += 'password = %s'
                    update_params.append(bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()))
                
                update_query += ' WHERE id = %s'
                update_params.append(session['user_id'])
                
                cursor.execute(update_query, update_params)
                mysql.connection.commit()
                cursor.close()
                
                # Update session username if changed
                if new_username:
                    session['user_name'] = new_username
                    
                flash('Settings updated successfully!', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Please provide a new username, password, or both to update.', 'error')
        else:
            flash('Incorrect current password.', 'error')

    return render_template('settings.html', form=form, current_username=current_username)

@app.route('/delete_account', methods=['POST'])
def delete_account():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    current_password = request.form.get('current_password')
    if not current_password:
        flash('Current password is required for deletion.', 'error')
        return redirect(url_for('settings'))
    
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT password FROM users WHERE id = %s', (session['user_id'],))
    user = cursor.fetchone()
    
    if user and bcrypt.checkpw(current_password.encode('utf-8'), user[0].encode('utf-8')):
        # Delete associated data
        cursor.execute('DELETE FROM user_notes WHERE user_id = %s', (session['user_id'],))
        cursor.execute('DELETE FROM quiz_attempts WHERE user_id = %s', (session['user_id'],))
        cursor.execute('DELETE FROM user_progress WHERE user_id = %s', (session['user_id'],))
        cursor.execute('DELETE FROM users WHERE id = %s', (session['user_id'],))
        mysql.connection.commit()
        cursor.close()
        session.clear()
        flash('Account deleted successfully. Goodbye!', 'success')
        return redirect(url_for('index'))
    else:
        flash('Incorrect current password for account deletion.', 'error')
        return redirect(url_for('settings'))

@app.route('/logout')
def logout():
    session.clear() # Clears all session data
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/subjects')
def subjects():
    if 'user_id' not in session:
        flash('Please login first.', 'error')
        return redirect(url_for('login'))
    
    subjects_data = get_subjects_with_topics()
    return render_template('subjects.html', subjects=subjects_data)

if __name__ == '__main__':
    app.run(debug=True)