import cv2
import numpy as np
import tensorflow as tf
from datetime import datetime
import json
import time
import paho.mqtt.client as mqtt
import firebase_admin
from firebase_admin import credentials, firestore


# ===== CONFIG =====
MODEL_PATH = "waste_classifier.tflite"
CLASSES = ['Hazardous', 'Organic', 'Recyclable']
INPUT_SIZE = (224, 224)

IP_ADDRESS = "192.168.213.134"    # DroidCam IP (change this)
PORT = "4747"
STREAM_URLS = [
    f"http://{IP_ADDRESS}:{PORT}/video",
    f"http://{IP_ADDRESS}:{PORT}/mjpegfeed"
]

# ===== MQTT CONFIG =====
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
TOPIC_REQUEST = "futurecan/capture_request"
TOPIC_RESULT  = "futurecan/waste_class"

# ===== FIREBASE CONFIG =====
cred = credentials.Certificate('serviceAccountKey.json') 
firebase_admin.initialize_app(cred)
fs_client = firestore.client()  # Create a Firestore client
print("[INFO] Connected to Cloud Firestore.")

# ===== LOAD MODEL =====
print("[INFO] Loading TensorFlow Lite model...")
interpreter = tf.lite.Interpreter(model_path=MODEL_PATH)
interpreter.allocate_tensors()
input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()
print("[INFO] Model ready.")

# ===== OPEN CAMERA =====
cap = None
for url in STREAM_URLS:
    print(f"[INFO] Trying camera stream: {url}")
    cap = cv2.VideoCapture(url)
    if cap.isOpened():
        print(f"✅ Connected to {url}")
        break
    else:
        print(f"❌ Failed to open {url}")

if not cap or not cap.isOpened():
    print("[ERROR] No camera stream found. Check DroidCam IP.")
    exit()

# ===== MQTT CALLBACKS =====
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"✅ Connected to MQTT Broker at {MQTT_BROKER}")
        client.subscribe(TOPIC_REQUEST)
        print(f"📡 Subscribed to topic: {TOPIC_REQUEST}")
    else:
        print(f"[ERROR] Connection failed with code {rc}")

# This is your 'on_message' function with the hard-coded user ID
def on_message(client, userdata, msg):
    print("\n🔔 Capture request received!")
    print(f"Topic: {msg.topic}")

    try:
        # 1. Decode the payload from the ESP32
        payload_str = msg.payload.decode()
        print(f"Payload: {payload_str}") # Print the raw payload
        
        data = json.loads(payload_str)
        
        # 2. Get the User ID from the JSON
        user_id = 'SETVwToKgMV8qRfS9kLuQgjIdy92' # Your hard-coded ID
        
        if not user_id:
            print(f"[ERROR] 'uid' not found in payload: {payload_str}")
            return
            
        print(f"Request received for user: {user_id}")
        
        # 3. Pass this ID to the inference function
        run_inference_and_publish(user_id) # <--- CHANGED: Pass user_id

    except json.JSONDecodeError:
        print(f"[ERROR] Invalid JSON from ESP32: {msg.payload.decode()}")
    except Exception as e:
        print(f"[ERROR] in on_message: {e}")

# ===== FUNCTION: Capture and classify =====
def run_inference_and_publish(user_id):
    print("📸 Starting camera capture...")

    # Countdown display
    start_time = time.time()
    while True:
        ret, frame = cap.read()
        if not ret:
            print("[WARN] Frame not captured.")
            continue

        elapsed = time.time() - start_time
        remaining = 3 - int(elapsed)
        if remaining > 0:
            cv2.putText(frame, f"Capturing in {remaining}s", (50, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
        cv2.imshow("FutureCan Capture", frame)

        if elapsed >= 3:
            break

        if cv2.waitKey(1) & 0xFF == ord('q'):
            print("🛑 Cancelled manually.")
            return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S") # This is the STRING timestamp
    print(f"📸 Capturing frame at {timestamp}...")

    img_resized = cv2.resize(frame, INPUT_SIZE)
    img_rgb = cv2.cvtColor(img_resized, cv2.COLOR_BGR2RGB)
    input_data = np.expand_dims(img_rgb.astype(np.float32), axis=0)

    # Run inference
    interpreter.set_tensor(input_details[0]['index'], input_data)
    interpreter.invoke()
    preds = interpreter.get_tensor(output_details[0]['index'])[0]

    class_idx = int(np.argmax(preds))
    confidence = float(preds[class_idx]) * 100.0
    class_name = CLASSES[class_idx]

    print(f"\n🧠 Prediction: {class_name} ({confidence:.2f}%)")

    # -----------------------------------------------------------------
    # <--- START OF UPDATED BLOCK (Fix for JSON error) ---
    # -----------------------------------------------------------------

    # 1. Create and Send Payload for MQTT (Simple Data Only)
    #    We use the STRING 'timestamp' here because it's JSON serializable
    mqtt_payload = {
        "timestamp": timestamp, 
        "primary_category": class_name,
        "item_name": round(confidence, 2)
    }
    msg = json.dumps(mqtt_payload)
    client.publish(TOPIC_RESULT, msg)
    print(f"📤 Sent MQTT result to '{TOPIC_RESULT}': {msg}")


    # 2. Create and Send Payload for FIREBASE (with Server Timestamp)
    #    We use 'firestore.SERVER_TIMESTAMP' here for the React app
    firebase_payload = {
        "timestamp": firestore.SERVER_TIMESTAMP, # The REAL timestamp object
        "primary_category": class_name,
        "item_name": round(confidence, 2) 
    }

    try:
        # Use the 'firebase_payload' to update Firestore
        fs_client.collection('users').document(user_id).collection('waste_logs').add(firebase_payload)
        
        print(f"🔥 Successfully logged to Firestore for user: {user_id}")
    except Exception as e:
        print(f"❌ ERROR writing to Cloud Firestore for user {user_id}: {e}")
    
    # -----------------------------------------------------------------
    # <--- END OF UPDATED BLOCK ---
    # -----------------------------------------------------------------

    # Show image
    label_text = f"{class_name} ({confidence:.1f}%)"
    cv2.putText(frame, label_text, (10, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3)
    cv2.imshow("FutureCan Result", frame)
    cv2.waitKey(2000)

# ===== MAIN =====
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1) 
client.on_connect = on_connect
client.on_message = on_message

print("[INFO] Connecting to MQTT broker...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

try:
    print("🚀 Ready. Waiting for ESP32 capture requests...")
    client.loop_forever()
except KeyboardInterrupt:
    print("\n[STOP] Closing camera and MQTT...")
    cap.release()
    cv2.destroyAllWindows()
    client.disconnect()
