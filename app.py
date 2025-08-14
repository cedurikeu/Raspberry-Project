(cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF'
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,426 @@
+import os
+import sqlite3
+import threading
+import time
+from datetime import datetime, timedelta
+from functools import wraps
+
+from dotenv import load_dotenv
+from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash
+from flask_socketio import SocketIO, emit
+from flask_cors import CORS
+from pymongo import MongoClient
+import random
+import math
+
+# Load environment variables
+load_dotenv()
+
+# Constants
+DEVICE_ID = "raspberry-pi-01"
+SENSOR_READ_INTERVAL = 10  # seconds
+ALERT_THRESHOLDS = {
+    'temperature': 30.0,
+    'humidity': 70.0,
+    'co2': 1000,
+    'water_level': 20.0
+}
+
+# Initialize Flask
+app = Flask(__name__)
+CORS(app)
+app.config.update({
+    'SECRET_KEY': os.getenv('SECRET_KEY', 'your-secret-key-here'),
+    'MONGO_URI': os.getenv('MONGODB_URI'),
+    'DASHBOARD_PASSWORD': os.getenv('DASHBOARD_PASSWORD', 'admin123')  # Change this!
+})
+
+# Initialize SocketIO
+socketio = SocketIO(
+    app,
+    cors_allowed_origins="*",
+    async_mode='threading',
+    logger=False,
+    engineio_logger=False
+)
+
+# ========================
+# AUTHENTICATION
+# ========================
+def require_auth(f):
+    @wraps(f)
+    def decorated_function(*args, **kwargs):
+        if 'authenticated' not in session:
+            return redirect(url_for('login'))
+        return f(*args, **kwargs)
+    return decorated_function
+
+# ========================
+# DATABASE SERVICE
+# ========================
+class DatabaseService:
+    def __init__(self):
+        self.mongo_client = None
+        self.mongo_db = None
+        self.sqlite_conn = None
+        self.use_mongo = True
+        self.setup_databases()
+        
+    def setup_databases(self):
+        # Setup MongoDB
+        self.connect_mongodb()
+        
+        # Setup SQLite fallback
+        self.setup_sqlite()
+        
+    def connect_mongodb(self):
+        try:
+            if app.config['MONGO_URI']:
+                self.mongo_client = MongoClient(app.config['MONGO_URI'], serverSelectionTimeoutMS=5000)
+                self.mongo_db = self.mongo_client.sensor_db
+                # Test connection
+                self.mongo_client.server_info()
+                self.use_mongo = True
+                print("✅ MongoDB connected")
+                return True
+        except Exception as e:
+            print(f"❌ MongoDB connection failed: {e}")
+            self.use_mongo = False
+            self.mongo_db = None
+            return False
+            
+    def setup_sqlite(self):
+        try:
+            self.sqlite_conn = sqlite3.connect('sensor_data.db', check_same_thread=False)
+            cursor = self.sqlite_conn.cursor()
+            cursor.execute('''
+                CREATE TABLE IF NOT EXISTS readings (
+                    id INTEGER PRIMARY KEY AUTOINCREMENT,
+                    device_id TEXT,
+                    temperature REAL,
+                    humidity REAL,
+                    co2 INTEGER,
+                    timestamp TEXT,
+                    server_timestamp TEXT
+                )
+            ''')
+            cursor.execute('''
+                CREATE TABLE IF NOT EXISTS config (
+                    id INTEGER PRIMARY KEY AUTOINCREMENT,
+                    device_id TEXT UNIQUE,
+                    config_data TEXT,
+                    updated_at TEXT
+                )
+            ''')
+            self.sqlite_conn.commit()
+            print("✅ SQLite database initialized")
+        except Exception as e:
+            print(f"❌ SQLite setup failed: {e}")
+
+    def save_reading(self, data):
+        # Try MongoDB first, fallback to SQLite
+        if self.use_mongo and self.mongo_db:
+            try:
+                data['server_timestamp'] = datetime.utcnow()
+                result = self.mongo_db.readings.insert_one(data)
+                print(f"📦 Saved to MongoDB: {result.inserted_id}")
+                return result.inserted_id
+            except Exception as e:
+                print(f"📦 MongoDB save error, switching to SQLite: {e}")
+                self.use_mongo = False
+                
+        # SQLite fallback
+        if self.sqlite_conn:
+            try:
+                cursor = self.sqlite_conn.cursor()
+                cursor.execute('''
+                    INSERT INTO readings (device_id, temperature, humidity, co2, timestamp, server_timestamp)
+                    VALUES (?, ?, ?, ?, ?, ?)
+                ''', (
+                    data['device_id'],
+                    data['temperature'],
+                    data['humidity'],
+                    data.get('co2', 400),
+                    data['timestamp'],
+                    datetime.utcnow().isoformat()
+                ))
+                self.sqlite_conn.commit()
+                print(f"📦 Saved to SQLite: {cursor.lastrowid}")
+                return cursor.lastrowid
+            except Exception as e:
+                print(f"📦 SQLite save error: {e}")
+                return None
+
+    def get_latest_readings(self, limit=10):
+        # Try MongoDB first, fallback to SQLite
+        if self.use_mongo and self.mongo_db:
+            try:
+                cursor = self.mongo_db.readings.find({
+                    'device_id': DEVICE_ID
+                }).sort('server_timestamp', -1).limit(limit)
+                
+                readings = []
+                for doc in cursor:
+                    doc['_id'] = str(doc['_id'])
+                    if isinstance(doc.get('server_timestamp'), datetime):
+                        doc['server_timestamp'] = doc['server_timestamp'].isoformat()
+                    readings.append(doc)
+                
+                print(f"📊 Retrieved {len(readings)} readings from MongoDB")
+                return readings
+            except Exception as e:
+                print(f"📦 MongoDB read error, switching to SQLite: {e}")
+                self.use_mongo = False
+                
+        # SQLite fallback
+        if self.sqlite_conn:
+            try:
+                cursor = self.sqlite_conn.cursor()
+                cursor.execute('''
+                    SELECT * FROM readings 
+                    WHERE device_id = ? 
+                    ORDER BY server_timestamp DESC 
+                    LIMIT ?
+                ''', (DEVICE_ID, limit))
+                
+                columns = [description[0] for description in cursor.description]
+                readings = [dict(zip(columns, row)) for row in cursor.fetchall()]
+                print(f"📊 Retrieved {len(readings)} readings from SQLite")
+                return readings
+            except Exception as e:
+                print(f"📦 SQLite read error: {e}")
+                return []
+                
+        return []
+
+    def get_historical_data(self, hours=24, limit=500):
+        time_threshold = datetime.utcnow() - timedelta(hours=hours)
+        
+        # Try MongoDB first, fallback to SQLite
+        if self.use_mongo and self.mongo_db:
+            try:
+                cursor = self.mongo_db.readings.find({
+                    'server_timestamp': {'$gte': time_threshold},
+                    'device_id': DEVICE_ID
+                }).sort('server_timestamp', -1).limit(limit)
+                
+                readings = []
+                for doc in cursor:
+                    doc['_id'] = str(doc['_id'])
+                    if isinstance(doc.get('server_timestamp'), datetime):
+                        doc['server_timestamp'] = doc['server_timestamp'].isoformat()
+                    readings.append(doc)
+                
+                return readings
+            except Exception as e:
+                print(f"📦 MongoDB historical read error: {e}")
+                self.use_mongo = False
+                
+        # SQLite fallback
+        if self.sqlite_conn:
+            try:
+                cursor = self.sqlite_conn.cursor()
+                cursor.execute('''
+                    SELECT * FROM readings 
+                    WHERE device_id = ? AND server_timestamp >= ?
+                    ORDER BY server_timestamp DESC 
+                    LIMIT ?
+                ''', (DEVICE_ID, time_threshold.isoformat(), limit))
+                
+                columns = [description[0] for description in cursor.description]
+                return [dict(zip(columns, row)) for row in cursor.fetchall()]
+            except Exception as e:
+                print(f"📦 SQLite historical read error: {e}")
+                return []
+                
+        return []
+
+# Initialize DB service
+db_service = DatabaseService()
+
+# ========================
+# SENSOR SERVICE
+# ========================
+class SensorService:
+    def __init__(self):
+        self.simulation_time = 0
+        self.current_data = {
+            'temperature': 0,
+            'humidity': 0,
+            'co2': 400,
+            'timestamp': datetime.utcnow().isoformat(),
+            'device_id': DEVICE_ID
+        }
+        
+    def simulate_realistic_data(self):
+        # Create more realistic sensor simulation
+        self.simulation_time += SENSOR_READ_INTERVAL
+        
+        # Temperature with daily cycle
+        hour = (time.time() / 3600) % 24
+        temp_base = 24.0  # Base temperature
+        daily_variation = 3 * math.sin((hour - 6) * math.pi / 12)  # Peak at 2 PM
+        temp_noise = random.uniform(-0.5, 0.5)
+        temperature = temp_base + daily_variation + temp_noise
+        
+        # Humidity inversely related to temperature
+        humidity_base = 65.0
+        temp_effect = -1.2 * (temperature - temp_base)
+        humidity_noise = random.uniform(-2, 2)
+        humidity = max(30, min(90, humidity_base + temp_effect + humidity_noise))
+        
+        # CO2 with some variation
+        co2_base = 800
+        co2_variation = random.uniform(-100, 150)
+        co2 = max(400, co2_base + co2_variation)
+        
+        self.current_data = {
+            'temperature': round(temperature, 1),
+            'humidity': round(humidity, 1),
+            'co2': int(co2),
+            'timestamp': datetime.utcnow().isoformat(),
+            'device_id': DEVICE_ID
+        }
+        
+        return self.current_data
+
+# Initialize sensor service
+sensor_service = SensorService()
+
+# ========================
+# WEB ROUTES
+# ========================
+@app.route('/login', methods=['GET', 'POST'])
+def login():
+    if request.method == 'POST':
+        password = request.form['password']
+        if password == app.config['DASHBOARD_PASSWORD']:
+            session['authenticated'] = True
+            return redirect(url_for('dashboard'))
+        else:
+            flash('Invalid password')
+    return render_template('login.html')
+
+@app.route('/logout')
+def logout():
+    session.pop('authenticated', None)
+    return redirect(url_for('login'))
+
+@app.route('/')
+@require_auth
+def dashboard():
+    return render_template('dashboard.html')
+
+@app.route('/api/current')
+@require_auth
+def get_current():
+    return jsonify(sensor_service.current_data)
+
+@app.route('/api/latest')
+@require_auth
+def get_latest():
+    readings = db_service.get_latest_readings(limit=10)
+    return jsonify(readings)
+
+@app.route('/api/history')
+@require_auth
+def get_history():
+    hours = request.args.get('hours', 24, type=int)
+    readings = db_service.get_historical_data(hours)
+    return jsonify(readings)
+
+@app.route('/api/status')
+@require_auth
+def get_status():
+    return jsonify({
+        'database': 'MongoDB' if db_service.use_mongo else 'SQLite',
+        'connected': db_service.use_mongo or db_service.sqlite_conn is not None,
+        'device_id': DEVICE_ID
+    })
+
+# SocketIO Handlers
+@socketio.on('connect')
+def handle_connect():
+    if 'authenticated' in session:
+        emit('sensor_update', sensor_service.current_data)
+        emit('status_update', {
+            'database': 'MongoDB' if db_service.use_mongo else 'SQLite',
+            'connected': True
+        })
+
+@socketio.on('request_data')
+def handle_data_request():
+    if 'authenticated' in session:
+        emit('sensor_update', sensor_service.current_data)
+
+# ========================
+# BACKGROUND TASKS
+# ========================
+def sensor_monitor():
+    """Background task to simulate sensor readings and save to database"""
+    while True:
+        try:
+            # Generate sensor data
+            sensor_data = sensor_service.simulate_realistic_data()
+            
+            # Save to database (MongoDB or SQLite fallback)
+            db_service.save_reading(sensor_data)
+            
+            # Emit real-time update to connected clients
+            socketio.emit('sensor_update', sensor_data)
+            
+            # Log current values
+            print(f"📊 T: {sensor_data['temperature']}°C, H: {sensor_data['humidity']}%, CO2: {sensor_data['co2']}ppm")
+            
+            time.sleep(SENSOR_READ_INTERVAL)
+            
+        except Exception as e:
+            print(f"⚠️ Sensor monitor error: {e}")
+            time.sleep(5)
+
+def database_health_monitor():
+    """Monitor database connections and switch between MongoDB and SQLite"""
+    while True:
+        try:
+            # Try to reconnect to MongoDB if we're using SQLite
+            if not db_service.use_mongo:
+                if db_service.connect_mongodb():
+                    print("🔄 Switched back to MongoDB")
+                    
+            time.sleep(30)  # Check every 30 seconds
+            
+        except Exception as e:
+            print(f"⚠️ Database monitor error: {e}")
+            time.sleep(30)
+
+# ========================
+# MAIN APPLICATION
+# ========================
+def open_browser():
+    """Open browser after a short delay to allow server to start"""
+    import webbrowser
+    time.sleep(3)  # Wait for server to start
+    webbrowser.open('http://localhost:5000')
+    print("🌐 Browser opened automatically")
+
+def main():
+    print("🚀 Starting IoT Monitoring System")
+    print(f"📊 Database: {'MongoDB' if db_service.use_mongo else 'SQLite'}")
+    
+    # Start background threads
+    threading.Thread(target=sensor_monitor, daemon=True).start()
+    threading.Thread(target=database_health_monitor, daemon=True).start()
+    threading.Thread(target=open_browser, daemon=True).start()  # Auto-open browser
+    
+    # Start web server
+    try:
+        print("🌐 Web server running at http://localhost:5000")
+        print(f"🔐 Dashboard password: {app.config['DASHBOARD_PASSWORD']}")
+        socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False)
+    except KeyboardInterrupt:
+        print("🛑 Server stopped")
+    except Exception as e:
+        print(f"❌ Server error: {e}")
+
+if __name__ == '__main__':
+    main()
EOF
)
