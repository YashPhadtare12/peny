from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
from datetime import datetime, timedelta
import os
import pandas as pd
from io import BytesIO

# Initialize Flask app
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key')

# Configure upload folder for Vercel
app.config['UPLOAD_FOLDER'] = '/tmp/images/doctors'
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg'}
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max upload size

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Helper functions
def get_db_connection():
    """Create and return a database connection with row factory."""
    conn = sqlite3.connect('/tmp/hospital.db')
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def generate_time_slots(start_time, end_time, break_start=None, break_end=None, interval=15):
    slots = []
    current_time = datetime.strptime(start_time, '%H:%M')
    end_time = datetime.strptime(end_time, '%H:%M')
    
    while current_time < end_time:
        slot_end = current_time + timedelta(minutes=interval)
        
        if break_start and break_end:
            break_start_time = datetime.strptime(break_start, '%H:%M')
            break_end_time = datetime.strptime(break_end, '%H:%M')
            if current_time >= break_start_time and slot_end <= break_end_time:
                current_time = break_end_time
                continue
        
        if slot_end <= end_time:
            slots.append({
                'start': current_time.strftime('%H:%M'),
                'end': slot_end.strftime('%H:%M'),
                'display_start': current_time.strftime('%I:%M %p').lower(),
                'display_end': slot_end.strftime('%I:%M %p').lower()
            })
        current_time = slot_end
    
    return slots

def init_db():
    """Initialize database if not exists"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Create tables if they don't exist
        cursor.executescript('''
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            hospital_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            specialization TEXT NOT NULL,
            experience INTEGER,
            consultation_fee REAL,
            contact TEXT,
            bio TEXT,
            image_path TEXT,
            username TEXT UNIQUE,
            password TEXT,
            created_by INTEGER,
            hospital_id INTEGER NOT NULL,
            FOREIGN KEY (created_by) REFERENCES staff(id),
            FOREIGN KEY (hospital_id) REFERENCES staff(id)
        );

        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            gender TEXT,
            contact TEXT,
            address TEXT,
            medical_history TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER,
            hospital_id INTEGER NOT NULL,
            FOREIGN KEY (created_by) REFERENCES staff(id),
            FOREIGN KEY (hospital_id) REFERENCES staff(id)
        );

        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            status TEXT DEFAULT 'Scheduled',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hospital_id INTEGER NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id),
            FOREIGN KEY (hospital_id) REFERENCES staff(id)
        );

        CREATE TABLE IF NOT EXISTS prescriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL UNIQUE,
            diagnosis TEXT NOT NULL,
            medicines TEXT NOT NULL,
            instructions TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hospital_id INTEGER NOT NULL,
            FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE CASCADE,
            FOREIGN KEY (hospital_id) REFERENCES staff(id)
        );

        CREATE TABLE IF NOT EXISTS doctor_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            break_start TEXT,
            break_end TEXT,
            hospital_id INTEGER NOT NULL,
            FOREIGN KEY (doctor_id) REFERENCES doctors(id),
            FOREIGN KEY (hospital_id) REFERENCES staff(id)
        );
        ''')

        # Create default admin if not exists
        cursor.execute("SELECT COUNT(*) FROM staff WHERE email = 'admin@hospital.com'")
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                'INSERT INTO staff (name, email, password, hospital_name) VALUES (?, ?, ?, ?)',
                ('Admin', 'admin@hospital.com', generate_password_hash('admin123'), 'City General Hospital')
            )
        
        conn.commit()
    except Exception as e:
        print(f"Database initialization error: {str(e)}")
    finally:
        conn.close()

# Initialize database on startup
init_db()

# Authentication routes
@app.route('/')
def home():
    if 'user_id' in session:
        if session['user_type'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('doctor_dashboard'))
    return render_template('auth/login.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user_type = request.form.get('user_type', '').strip()
        
        if not all([username, password, user_type]):
            flash('All fields are required', 'danger')
            return redirect(url_for('login'))
        
        conn = get_db_connection()
        user = None
        
        try:
            if user_type == 'admin':
                user = conn.execute('SELECT * FROM staff WHERE email = ?', (username,)).fetchone()
                if user and check_password_hash(user['password'], password):
                    session.clear()
                    session['user_id'] = user['id']
                    session['user_type'] = user_type
                    session['name'] = user['name']
                    session['hospital_id'] = user['id']
                    session['hospital_name'] = user['hospital_name']
                    return redirect(url_for('admin_dashboard'))
            else:
                user = conn.execute('''
                    SELECT d.*, s.hospital_name 
                    FROM doctors d
                    JOIN staff s ON d.hospital_id = s.id
                    WHERE d.username = ? AND d.password IS NOT NULL
                ''', (username,)).fetchone()
                if user and check_password_hash(user['password'], password):
                    session.clear()
                    session['user_id'] = user['id']
                    session['user_type'] = user_type
                    session['name'] = user['name']
                    session['hospital_id'] = user['hospital_id']
                    session['hospital_name'] = user['hospital_name']
                    return redirect(url_for('doctor_dashboard'))
            
            flash('Invalid credentials or account not setup', 'danger')
        except Exception as e:
            flash('Login error occurred', 'danger')
            print(f"Login error: {str(e)}")
        finally:
            conn.close()
    
    return render_template('auth/login.html')

@app.route('/doctor/login', methods=['GET', 'POST'])
def doctor_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not all([username, password]):
            flash('All fields are required', 'danger')
            return redirect(url_for('doctor_login'))
        
        conn = get_db_connection()
        try:
            doctor = conn.execute('''
                SELECT d.*, s.hospital_name 
                FROM doctors d
                JOIN staff s ON d.hospital_id = s.id
                WHERE d.username = ? AND d.password IS NOT NULL
            ''', (username,)).fetchone()
            
            if doctor and check_password_hash(doctor['password'], password):
                session.clear()
                session['user_id'] = doctor['id']
                session['user_type'] = 'doctor'
                session['name'] = doctor['name']
                session['hospital_id'] = doctor['hospital_id']
                session['hospital_name'] = doctor['hospital_name']
                return redirect(url_for('doctor_dashboard'))
            
            flash('Invalid credentials or account not setup', 'danger')
        except Exception as e:
            flash('Login error occurred', 'danger')
            print(f"Doctor login error: {str(e)}")
        finally:
            conn.close()
    
    return render_template('doctor/login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        hospital_name = request.form.get('hospital_name', '').strip()
        
        if not all([name, email, password, hospital_name]):
            flash('All fields are required', 'danger')
            return redirect(url_for('register'))
        
        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO staff (name, email, password, hospital_name) VALUES (?, ?, ?, ?)',
                         (name, email, generate_password_hash(password), hospital_name))
            conn.commit()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email already exists!', 'danger')
        finally:
            conn.close()
    
    return render_template('auth/register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# Admin routes
@app.route('/admin/dashboard')
def admin_dashboard():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    doctors_count = conn.execute('SELECT COUNT(*) FROM doctors WHERE hospital_id = ?', 
                               (session['hospital_id'],)).fetchone()[0]
    patients_count = conn.execute('SELECT COUNT(*) FROM patients WHERE hospital_id = ?', 
                                (session['hospital_id'],)).fetchone()[0]
    appointments_count = conn.execute('SELECT COUNT(*) FROM appointments WHERE hospital_id = ?', 
                                    (session['hospital_id'],)).fetchone()[0]
    
    recent_appointments = conn.execute('''
        SELECT a.id, p.name as patient_name, d.name as doctor_name, a.date, a.time_slot, a.status
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        JOIN doctors d ON a.doctor_id = d.id
        WHERE a.hospital_id = ?
        ORDER BY a.date DESC, a.time_slot DESC
        LIMIT 5
    ''', (session['hospital_id'],)).fetchall()
    
    conn.close()
    
    return render_template('admin/dashboard.html', 
                         doctors_count=doctors_count,
                         patients_count=patients_count,
                         appointments_count=appointments_count,
                         recent_appointments=recent_appointments)

@app.route('/admin/add_patient', methods=['GET', 'POST'])
def add_patient():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        age = request.form.get('age', '').strip()
        gender = request.form.get('gender', '').strip()
        contact = request.form.get('contact', '').strip()
        address = request.form.get('address', '').strip()
        medical_history = request.form.get('medical_history', '').strip()
        
        if not all([name, age, gender, contact]):
            flash('Required fields are missing', 'danger')
            return redirect(url_for('add_patient'))
        
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO patients (name, age, gender, contact, address, medical_history, created_by, hospital_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (name, age, gender, contact, address, medical_history, session['user_id'], session['hospital_id']))
            conn.commit()
            flash('Patient added successfully!', 'success')
            return redirect(url_for('view_patients'))
        except Exception as e:
            flash('Error adding patient', 'danger')
            print(f"Add patient error: {str(e)}")
        finally:
            conn.close()
    
    return render_template('admin/add_patient.html')

@app.route('/admin/add_doctor', methods=['GET', 'POST'])
def add_doctor():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        specialization = request.form.get('specialization', '').strip()
        experience = request.form.get('experience', '').strip()
        consultation_fee = request.form.get('consultation_fee', '').strip()
        contact = request.form.get('contact', '').strip()
        bio = request.form.get('bio', '').strip()
        
        if not all([name, specialization, experience, consultation_fee, contact]):
            flash('Required fields are missing', 'danger')
            return redirect(url_for('add_doctor'))
        
        image_path = None
        if 'image' in request.files:
            file = request.files['image']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(image_path)
                image_path = 'images/doctors/' + filename
        
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO doctors (name, specialization, experience, consultation_fee, contact, bio, image_path, created_by, hospital_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (name, specialization, experience, consultation_fee, contact, bio, image_path, session['user_id'], session['hospital_id']))
            conn.commit()
            flash('Doctor added successfully!', 'success')
            return redirect(url_for('view_doctors'))
        except Exception as e:
            flash('Error adding doctor', 'danger')
            print(f"Add doctor error: {str(e)}")
        finally:
            conn.close()
    
    return render_template('admin/add_doctor.html')

@app.route('/admin/view_patients')
def view_patients():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    search_query = request.args.get('search', '').strip()
    
    conn = get_db_connection()
    try:
        if search_query:
            patients = conn.execute('''
                SELECT * FROM patients 
                WHERE (name LIKE ? OR contact LIKE ?) AND hospital_id = ?
                ORDER BY name
            ''', (f'%{search_query}%', f'%{search_query}%', session['hospital_id'])).fetchall()
        else:
            patients = conn.execute('SELECT * FROM patients WHERE hospital_id = ? ORDER BY name', 
                                   (session['hospital_id'],)).fetchall()
    except Exception as e:
        flash('Error loading patients', 'danger')
        print(f"View patients error: {str(e)}")
        patients = []
    finally:
        conn.close()
    
    return render_template('admin/view_patients.html', 
                         patients=patients, 
                         search_query=search_query)

@app.route('/admin/view_doctors')
def view_doctors():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    search_query = request.args.get('search', '').strip()
    
    conn = get_db_connection()
    try:
        if search_query:
            doctors = conn.execute('''
                SELECT * FROM doctors 
                WHERE (name LIKE ? OR specialization LIKE ?) AND hospital_id = ?
                ORDER BY name
            ''', (f'%{search_query}%', f'%{search_query}%', session['hospital_id'])).fetchall()
        else:
            doctors = conn.execute('SELECT * FROM doctors WHERE hospital_id = ? ORDER BY name', 
                                  (session['hospital_id'],)).fetchall()
    except Exception as e:
        flash('Error loading doctors', 'danger')
        print(f"View doctors error: {str(e)}")
        doctors = []
    finally:
        conn.close()
    
    return render_template('admin/view_doctors.html', 
                         doctors=doctors, 
                         search_query=search_query)

@app.route('/admin/set_doctor_credentials/<int:doctor_id>', methods=['GET', 'POST'])
def set_doctor_credentials(doctor_id):
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    doctor = conn.execute('SELECT * FROM doctors WHERE id = ? AND hospital_id = ?', 
                         (doctor_id, session['hospital_id'])).fetchone()
    
    if not doctor:
        flash('Doctor not found', 'danger')
        return redirect(url_for('view_doctors'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if len(username) < 4:
            flash('Username must be at least 4 characters', 'danger')
            return redirect(url_for('set_doctor_credentials', doctor_id=doctor_id))
        
        if len(password) < 8:
            flash('Password must be at least 8 characters', 'danger')
            return redirect(url_for('set_doctor_credentials', doctor_id=doctor_id))
        
        try:
            existing = conn.execute(
                'SELECT id FROM doctors WHERE username = ? AND id != ? AND hospital_id = ?',
                (username, doctor_id, session['hospital_id'])
            ).fetchone()
            
            if existing:
                flash('Username already in use', 'danger')
                return redirect(url_for('set_doctor_credentials', doctor_id=doctor_id))
            
            conn.execute(
                'UPDATE doctors SET username = ?, password = ? WHERE id = ? AND hospital_id = ?',
                (username, generate_password_hash(password), doctor_id, session['hospital_id'])
            )
            conn.commit()
            flash('Doctor credentials updated successfully!', 'success')
            return redirect(url_for('view_doctors'))
        except sqlite3.Error as e:
            flash('Failed to update credentials', 'danger')
            print(f"Credential update error: {str(e)}")
        finally:
            conn.close()
    
    return render_template('admin/set_doctor_credentials.html', doctor=doctor)

@app.route('/admin/set_doctor_slots/<int:doctor_id>', methods=['GET', 'POST'])
def set_doctor_slots(doctor_id):
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    doctor = conn.execute('SELECT * FROM doctors WHERE id = ? AND hospital_id = ?', 
                         (doctor_id, session['hospital_id'])).fetchone()
    if not doctor:
        flash('Doctor not found', 'danger')
        return redirect(url_for('view_doctors'))
    
    if request.method == 'POST':
        day = request.form.get('day', '').strip()
        start_time = request.form.get('start_time', '').strip()
        end_time = request.form.get('end_time', '').strip()
        break_start = request.form.get('break_start', '').strip() or None
        break_end = request.form.get('break_end', '').strip() or None
        
        if not all([day, start_time, end_time]):
            flash('Required fields are missing', 'danger')
            return redirect(url_for('set_doctor_slots', doctor_id=doctor_id))
        
        try:
            conn.execute('DELETE FROM doctor_slots WHERE doctor_id = ? AND day = ? AND hospital_id = ?', 
                        (doctor_id, day, session['hospital_id']))
            conn.execute('''
                INSERT INTO doctor_slots (doctor_id, day, start_time, end_time, break_start, break_end, hospital_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (doctor_id, day, start_time, end_time, break_start, break_end, session['hospital_id']))
            conn.commit()
            flash('Doctor availability slots updated successfully!', 'success')
            return redirect(url_for('view_doctors'))
        except Exception as e:
            flash('Error updating slots', 'danger')
            print(f"Set slots error: {str(e)}")
        finally:
            conn.close()
    
    try:
        slots = conn.execute('''
            SELECT * FROM doctor_slots 
            WHERE doctor_id = ? AND hospital_id = ?
            ORDER BY 
                CASE day
                    WHEN 'Monday' THEN 1
                    WHEN 'Tuesday' THEN 2
                    WHEN 'Wednesday' THEN 3
                    WHEN 'Thursday' THEN 4
                    WHEN 'Friday' THEN 5
                    WHEN 'Saturday' THEN 6
                    WHEN 'Sunday' THEN 7
                END
        ''', (doctor_id, session['hospital_id'])).fetchall()
    except Exception as e:
        flash('Error loading doctor data', 'danger')
        print(f"Doctor slots error: {str(e)}")
        slots = []
    finally:
        conn.close()
    
    return render_template('admin/set_doctor_slots.html', 
                         doctor=doctor, 
                         slots=slots)

@app.route('/admin/get_doctor_slots/<int:doctor_id>/<date>')
def get_doctor_slots(doctor_id, date):
    if 'user_id' not in session or session['user_type'] != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        day = date_obj.strftime('%A')
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400
    
    conn = get_db_connection()
    try:
        slot_info = conn.execute('''
            SELECT * FROM doctor_slots 
            WHERE doctor_id = ? AND day = ? AND hospital_id = ?
        ''', (doctor_id, day, session['hospital_id'])).fetchone()
        
        if not slot_info:
            return jsonify({'error': 'Doctor not available on this day'})
        
        slots = generate_time_slots(
            slot_info['start_time'],
            slot_info['end_time'],
            slot_info['break_start'],
            slot_info['break_end']
        )
        
        booked_slots = conn.execute('''
            SELECT time_slot FROM appointments
            WHERE doctor_id = ? AND date = ? AND status != 'Cancelled' AND hospital_id = ?
        ''', (doctor_id, date, session['hospital_id'])).fetchall()
        
        booked_slots = [slot['time_slot'] for slot in booked_slots]
        
        return jsonify({
            'slots': slots,
            'booked_slots': booked_slots
        })
    except Exception as e:
        print(f"Get slots error: {str(e)}")
        return jsonify({'error': 'Server error'}), 500
    finally:
        conn.close()

@app.route('/admin/schedule_appointment', methods=['GET', 'POST'])
def schedule_appointment():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    if request.method == 'POST':
        patient_id = request.form.get('patient_id', '').strip()
        doctor_id = request.form.get('doctor_id', '').strip()
        date = request.form.get('date', '').strip()
        time_slot = request.form.get('time_slot', '').strip()
        notes = request.form.get('notes', '').strip()
        
        if not all([patient_id, doctor_id, date, time_slot]):
            flash('Required fields are missing', 'danger')
            return redirect(url_for('schedule_appointment'))
        
        try:
            conn.execute('''
                INSERT INTO appointments (patient_id, doctor_id, date, time_slot, notes, hospital_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (patient_id, doctor_id, date, time_slot, notes, session['hospital_id']))
            conn.commit()
            flash('Appointment scheduled successfully!', 'success')
            return redirect(url_for('view_appointments'))
        except Exception as e:
            flash('Error scheduling appointment', 'danger')
            print(f"Schedule appointment error: {str(e)}")
        finally:
            conn.close()
    
    try:
        patients = conn.execute('SELECT id, name FROM patients WHERE hospital_id = ? ORDER BY name', 
                              (session['hospital_id'],)).fetchall()
        doctors = conn.execute('SELECT id, name, specialization FROM doctors WHERE hospital_id = ? ORDER BY name', 
                             (session['hospital_id'],)).fetchall()
    except Exception as e:
        flash('Error loading data', 'danger')
        print(f"Schedule appointment load error: {str(e)}")
        patients = []
        doctors = []
    finally:
        conn.close()
    
    min_date = datetime.now().strftime('%Y-%m-%d')
    
    return render_template('admin/schedule_appointment.html', 
                         patients=patients, 
                         doctors=doctors,
                         min_date=min_date)

@app.route('/admin/view_appointments')
def view_appointments():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    search_query = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()
    
    conn = get_db_connection()
    try:
        query = '''
            SELECT a.id, p.name as patient_name, d.name as doctor_name, 
                   a.date, a.time_slot, a.status, a.notes
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN doctors d ON a.doctor_id = d.id
            WHERE a.hospital_id = ?
        '''
        
        params = [session['hospital_id']]
        
        if search_query:
            query += ' AND (p.name LIKE ? OR d.name LIKE ?)'
            params.extend([f'%{search_query}%', f'%{search_query}%'])
        
        if status_filter:
            query += ' AND a.status = ?'
            params.append(status_filter)
        
        query += ' ORDER BY a.date DESC, a.time_slot DESC'
        
        appointments = conn.execute(query, params).fetchall()
    except Exception as e:
        flash('Error loading appointments', 'danger')
        print(f"View appointments error: {str(e)}")
        appointments = []
    finally:
        conn.close()
    
    return render_template('admin/view_appointments.html', 
                         appointments=appointments,
                         search_query=search_query,
                         status_filter=status_filter)

@app.route('/admin/delete_appointment/<int:appointment_id>')
def delete_appointment(appointment_id):
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM appointments WHERE id = ? AND hospital_id = ?', 
                    (appointment_id, session['hospital_id']))
        conn.commit()
        flash('Appointment deleted successfully!', 'success')
    except Exception as e:
        flash('Error deleting appointment', 'danger')
        print(f"Delete appointment error: {str(e)}")
    finally:
        conn.close()
    
    return redirect(url_for('view_appointments'))

@app.route('/admin/export_appointments')
def export_appointments():
    if 'user_id' not in session or session['user_type'] != 'admin':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    try:
        appointments = conn.execute('''
            SELECT p.name as patient_name, d.name as doctor_name, 
                   a.date, a.time_slot, a.status, a.notes
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN doctors d ON a.doctor_id = d.id
            WHERE a.hospital_id = ?
            ORDER BY a.date DESC, a.time_slot DESC
        ''', (session['hospital_id'],)).fetchall()
        
        df = pd.DataFrame(appointments, columns=['Patient Name', 'Doctor Name', 'Date', 'Time Slot', 'Status', 'Notes'])
        
        output = BytesIO()
        writer = pd.ExcelWriter(output, engine='xlsxwriter')
        df.to_excel(writer, sheet_name='Appointments', index=False)
        writer.close()
        output.seek(0)
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='appointments.xlsx'
        )
    except Exception as e:
        flash('Error exporting appointments', 'danger')
        print(f"Export appointments error: {str(e)}")
        return redirect(url_for('view_appointments'))
    finally:
        conn.close()

# Doctor routes
@app.route('/doctor/dashboard')
def doctor_dashboard():
    if 'user_id' not in session or session['user_type'] != 'doctor':
        return redirect(url_for('login'))
    
    doctor_id = session['user_id']
    conn = get_db_connection()
    
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        appointments_today = conn.execute('''
            SELECT a.id, p.id as patient_id, p.name as patient_name, a.time_slot, a.status
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            WHERE a.doctor_id = ? AND a.date = ? AND a.hospital_id = ?
            ORDER BY a.time_slot
        ''', (doctor_id, today, session['hospital_id'])).fetchall()
        
        upcoming_appointments = conn.execute('''
            SELECT a.id, p.id as patient_id, p.name as patient_name, a.date, a.time_slot, a.status
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            WHERE a.doctor_id = ? AND a.date > ? AND a.hospital_id = ?
            ORDER BY a.date, a.time_slot
            LIMIT 5
        ''', (doctor_id, today, session['hospital_id'])).fetchall()
    except Exception as e:
        flash('Error loading dashboard data', 'danger')
        print(f"Doctor dashboard error: {str(e)}")
        appointments_today = []
        upcoming_appointments = []
    finally:
        conn.close()
    
    return render_template('doctor/dashboard.html',
                         appointments_today=appointments_today,
                         upcoming_appointments=upcoming_appointments)

@app.route('/doctor/appointments')
def doctor_appointments():
    if 'user_id' not in session or session['user_type'] != 'doctor':
        return redirect(url_for('login'))
    
    doctor_id = session['user_id']
    search_query = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()
    
    conn = get_db_connection()
    try:
        query = '''
            SELECT a.id, p.name as patient_name, p.age, p.gender,
                   a.date, a.time_slot, a.status, a.notes
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            WHERE a.doctor_id = ? AND a.hospital_id = ?
        '''
        
        params = [doctor_id, session['hospital_id']]
        
        if search_query:
            query += ' AND p.name LIKE ?'
            params.append(f'%{search_query}%')
        
        if status_filter:
            query += ' AND a.status = ?'
            params.append(status_filter)
        
        query += ' ORDER BY a.date DESC, a.time_slot DESC'
        
        appointments = conn.execute(query, params).fetchall()
    except Exception as e:
        flash('Error loading appointments', 'danger')
        print(f"Doctor appointments error: {str(e)}")
        appointments = []
    finally:
        conn.close()
    
    return render_template('doctor/view_appointments.html', 
                         appointments=appointments,
                         search_query=search_query,
                         status_filter=status_filter)

@app.route('/doctor/update_appointment_status/<int:appointment_id>', methods=['POST'])
def update_appointment_status(appointment_id):
    if 'user_id' not in session or session['user_type'] != 'doctor':
        return redirect(url_for('login'))
    
    status = request.form.get('status', '').strip()
    
    if not status:
        flash('Status is required', 'danger')
        return redirect(url_for('doctor_appointments'))
    
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE appointments 
            SET status = ? 
            WHERE id = ? AND doctor_id = ? AND hospital_id = ?
        ''', (status, appointment_id, session['user_id'], session['hospital_id']))
        conn.commit()
        flash('Appointment status updated successfully!', 'success')
    except Exception as e:
        flash('Error updating status', 'danger')
        print(f"Update status error: {str(e)}")
    finally:
        conn.close()
    
    return redirect(url_for('doctor_appointments'))

@app.route('/doctor/prescriptions/<int:appointment_id>', methods=['GET', 'POST'])
def prescriptions(appointment_id):
    if 'user_id' not in session or session['user_type'] != 'doctor':
        return redirect(url_for('login'))

    conn = get_db_connection()
    appointment = conn.execute('''
        SELECT a.*, p.name as patient_name, p.age, p.gender, p.medical_history,
               d.name as doctor_name, d.specialization
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        JOIN doctors d ON a.doctor_id = d.id
        WHERE a.id = ? AND a.hospital_id = ?
    ''', (appointment_id, session['hospital_id'])).fetchone()

    if not appointment:
        flash('Appointment not found', 'danger')
        return redirect(url_for('doctor_appointments'))

    if request.method == 'POST':
        diagnosis = request.form.get('diagnosis', '').strip()
        medicines = []
        
        medicine_count = int(request.form.get('medicine_count', 1))
        
        for i in range(1, medicine_count + 1):
            name = request.form.get(f'medicine_name_{i}', '').strip()
            dosage = request.form.get(f'medicine_dosage_{i}', '').strip()
            frequency = request.form.get(f'medicine_frequency_{i}', '').strip()
            
            if name and dosage and frequency:
                morning = '1' if request.form.get(f'medicine_morning_{i}') else '0'
                afternoon = '1' if request.form.get(f'medicine_afternoon_{i}') else '0'
                evening = '1' if request.form.get(f'medicine_evening_{i}') else '0'
                meal = request.form.get(f'medicine_meal_{i}', 'after')
                
                medicine_str = f"{name}|{dosage}|{frequency}|{morning}|{afternoon}|{evening}|{meal}"
                medicines.append(medicine_str)
        
        instructions = request.form.get('instructions', '').strip()
        medicines_str = '\n'.join(medicines) if medicines else ''

        try:
            existing = conn.execute('SELECT * FROM prescriptions WHERE appointment_id = ?', 
                                  (appointment_id,)).fetchone()
            if existing:
                conn.execute('''
                    UPDATE prescriptions 
                    SET diagnosis=?, medicines=?, instructions=?
                    WHERE appointment_id=?
                ''', (diagnosis, medicines_str, instructions, appointment_id))
            else:
                conn.execute('''
                    INSERT INTO prescriptions (appointment_id, diagnosis, medicines, instructions, hospital_id)
                    VALUES (?, ?, ?, ?, ?)
                ''', (appointment_id, diagnosis, medicines_str, instructions, session['hospital_id']))
            
            conn.commit()
            flash('Prescription saved successfully!', 'success')
            
            if request.form.get('action') == 'print':
                return redirect(url_for('print_prescription', appointment_id=appointment_id))
            return redirect(url_for('prescriptions', appointment_id=appointment_id))
        except Exception as e:
            flash('Error saving prescription', 'danger')
            print(f"Prescription save error: {str(e)}")
        finally:
            conn.close()

    # Get prescription if exists
    prescription = conn.execute('SELECT * FROM prescriptions WHERE appointment_id = ?', 
                               (appointment_id,)).fetchone()
    
    # Process prescription for display
    prescription_dict = None
    if prescription:
        prescription_dict = dict(prescription)
        medicines_parsed = []
        if prescription['medicines']:
            for medicine in prescription['medicines'].split('\n'):
                if medicine.strip():
                    parts = medicine.split('|')
                    if len(parts) == 7:  # Ensure we have all parts
                        medicines_parsed.append({
                            'name': parts[0],
                            'dosage': parts[1],
                            'frequency': parts[2],
                            'morning': parts[3],
                            'afternoon': parts[4],
                            'evening': parts[5],
                            'meal': parts[6]
                        })
        prescription_dict['medicines_parsed'] = medicines_parsed
    
    conn.close()
    
    return render_template('doctor/prescriptions.html',
                         appointment=appointment,
                         prescription=prescription_dict)

@app.route('/doctor/print_prescription/<int:appointment_id>')
def print_prescription(appointment_id):
    if 'user_id' not in session or session['user_type'] != 'doctor':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    try:
        prescription_data = conn.execute('''
            SELECT a.id as appointment_id, a.date, a.time_slot, 
                   p.name as patient_name, p.age, p.gender,
                   d.name as doctor_name, d.specialization,
                   pr.diagnosis, pr.medicines, pr.instructions,
                   s.hospital_name
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN doctors d ON a.doctor_id = d.id
            JOIN staff s ON d.hospital_id = s.id
            LEFT JOIN prescriptions pr ON a.id = pr.appointment_id
            WHERE a.id = ? AND a.hospital_id = ?
        ''', (appointment_id, session['hospital_id'])).fetchone()
        
        if not prescription_data:
            flash('Prescription not found!', 'danger')
            return redirect(url_for('doctor_appointments'))
        
        # Process medicines for display
        medicines_parsed = []
        if prescription_data['medicines']:
            for medicine in prescription_data['medicines'].split('\n'):
                if medicine.strip():
                    parts = medicine.split('|')
                    if len(parts) == 7:
                        medicines_parsed.append({
                            'name': parts[0],
                            'dosage': parts[1],
                            'frequency': parts[2],
                            'morning': parts[3],
                            'afternoon': parts[4],
                            'evening': parts[5],
                            'meal': parts[6]
                        })
        
        # Create a dictionary with all prescription data
        prescription = dict(prescription_data)
        prescription['medicines_parsed'] = medicines_parsed
        
        return render_template('doctor/print_prescription.html',
                            prescription=prescription)
    except Exception as e:
        flash('Error loading prescription', 'danger')
        print(f"Print prescription error: {str(e)}")
        return redirect(url_for('doctor_appointments'))
    finally:
        conn.close()

@app.route('/doctor/patients')
def all_patients_history():
    if 'user_id' not in session or session['user_type'] != 'doctor':
        return redirect(url_for('login'))
    
    search_query = request.args.get('search', '').strip()
    doctor_id = session['user_id']
    
    conn = get_db_connection()
    try:
        if search_query:
            patients = conn.execute('''
                SELECT p.*, MAX(a.date) as last_visit
                FROM patients p
                LEFT JOIN appointments a ON p.id = a.patient_id AND a.doctor_id = ?
                WHERE (p.name LIKE ? OR p.contact LIKE ?) AND p.hospital_id = ?
                GROUP BY p.id
                ORDER BY p.name
            ''', (doctor_id, f'%{search_query}%', f'%{search_query}%', session['hospital_id'])).fetchall()
        else:
            patients = conn.execute('''
                SELECT p.*, MAX(a.date) as last_visit
                FROM patients p
                LEFT JOIN appointments a ON p.id = a.patient_id AND a.doctor_id = ?
                WHERE p.hospital_id = ?
                GROUP BY p.id
                ORDER BY p.name
            ''', (doctor_id, session['hospital_id'])).fetchall()
    except Exception as e:
        flash('Error loading patients', 'danger')
        print(f"All patients history error: {str(e)}")
        patients = []
    finally:
        conn.close()
    
    return render_template('doctor/all_patients_history.html',
                         patients=patients,
                         search_query=search_query)

@app.route('/doctor/patient_history/<int:patient_id>')
def patient_history(patient_id):
    if 'user_id' not in session or session['user_type'] != 'doctor':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    try:
        # Get patient details
        patient = conn.execute('''
            SELECT * FROM patients 
            WHERE id = ? AND hospital_id = ?
        ''', (patient_id, session['hospital_id'])).fetchone()
        
        if not patient:
            flash('Patient not found', 'danger')
            return redirect(url_for('doctor_dashboard'))
        
        # Get all prescriptions
        prescriptions = conn.execute('''
            SELECT pr.*, a.date, a.time_slot, d.name as doctor_name
            FROM prescriptions pr
            JOIN appointments a ON pr.appointment_id = a.id
            JOIN doctors d ON a.doctor_id = d.id
            WHERE a.patient_id = ? AND a.hospital_id = ?
            ORDER BY a.date DESC
        ''', (patient_id, session['hospital_id'])).fetchall()
        
        # Process prescriptions for display
        prescriptions_list = []
        for prescription in prescriptions:
            pres_dict = dict(prescription)
            medicines_parsed = []
            if prescription['medicines']:
                for medicine in prescription['medicines'].split('\n'):
                    if medicine.strip():
                        parts = medicine.split('|')
                        if len(parts) == 7:
                            medicines_parsed.append({
                                'name': parts[0],
                                'dosage': parts[1],
                                'frequency': parts[2],
                                'morning': parts[3],
                                'afternoon': parts[4],
                                'evening': parts[5],
                                'meal': parts[6]
                            })
            pres_dict['medicines_parsed'] = medicines_parsed
            prescriptions_list.append(pres_dict)
        
        # Get all appointments
        appointments = conn.execute('''
            SELECT a.*, d.name as doctor_name
            FROM appointments a
            JOIN doctors d ON a.doctor_id = d.id
            WHERE a.patient_id = ? AND a.hospital_id = ?
            ORDER BY a.date DESC
        ''', (patient_id, session['hospital_id'])).fetchall()
        
        return render_template('doctor/patient_history.html',
                            patient=patient,
                            prescriptions=prescriptions_list,
                            appointments=appointments)
    except Exception as e:
        flash('Error loading patient history', 'danger')
        print(f"Patient history error: {str(e)}")
        return redirect(url_for('doctor_dashboard'))
    finally:
        conn.close()

# Health check endpoint for Vercel
@app.route('/health')
def health_check():
    try:
        conn = get_db_connection()
        conn.execute('SELECT 1')
        conn.close()
        return jsonify({'status': 'healthy'}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

# Error handlers
@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', error='Page not found'), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('error.html', error='Internal server error'), 500

if __name__ == '__main__':
    # This block won't run on Vercel, but useful for local development
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)