import xml.etree.ElementTree as ET
import requests
import sqlite3
import schedule
import time
from datetime import datetime
import re
import shutil
import os
import logging
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS
from functools import wraps

# Configure logging
logging.basicConfig(
    filename='tally_extract.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class TallyDataExtractor:
    def __init__(self):
        self.tally_url = "http://localhost:9000"
        self.db_path = "tally_data.db"
        self.backup_dir = "backups"
        self.setup_database()
        self.setup_api_server()
        
    def setup_database(self):
        try:
            if not os.path.exists(self.backup_dir):
                os.makedirs(self.backup_dir)
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('PRAGMA table_info(accounts)')
            columns = [row[1] for row in cursor.fetchall()]
            if 'accounts' not in [table[0] for table in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")] or 'due_date' not in columns:
                cursor.execute('DROP TABLE IF EXISTS accounts')
                cursor.execute('''
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL,
                        closing_balance REAL NOT NULL,
                        due_date TEXT,
                        parent_group TEXT,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                logging.info("Recreated accounts table with due_date column")
            conn.commit()
            logging.info("Database setup complete")
        except sqlite3.Error as e:
            logging.error(f"Database setup error: {e}")
        finally:
            conn.close()

    def setup_api_server(self):
        """Setup Flask API server for serving data"""
        self.app = Flask(__name__)
        # Updated CORS to allow your domain and fallback to wildcard
        CORS(self.app, resources={r"/api/*": {"origins": ["https://m.yourdomain.com", "*"]}}, supports_credentials=True)
        
        API_SECRET_KEY = os.environ.get('API_SECRET_KEY', 'TallyDash2024SecureKey789XYZ')
        logging.info(f"API server using key: {API_SECRET_KEY}")
        
        def verify_api_key(f):
            @wraps(f)
            def decorated_function(*args, **kwargs):
                logging.info(f"All headers: {dict(request.headers)}")
                api_key = request.headers.get('X-API-Key')
                logging.info(f"Received API key: {api_key}")
                if not api_key or api_key != API_SECRET_KEY:
                    return jsonify({'error': 'Invalid API key'}), 401
                return f(*args, **kwargs)
            return decorated_function

        @self.app.route('/api/health', methods=['GET'])
        def health_check():
            return jsonify({
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'database_exists': os.path.exists(self.db_path)
            })

        @self.app.route('/api/dashboard-data', methods=['GET'])
        @verify_api_key
        def get_dashboard_data():
            conn = self.get_db_connection()
            if not conn:
                return jsonify({'error': 'Database connection failed'}), 500
            
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT name, type, closing_balance, due_date, parent_group, last_updated
                    FROM accounts 
                    ORDER BY closing_balance DESC
                ''')
                accounts = [dict(row) for row in cursor.fetchall()]
                
                total_debtors = sum(acc['closing_balance'] for acc in accounts if acc['type'] == 'debtor')
                total_creditors = sum(acc['closing_balance'] for acc in accounts if acc['type'] == 'creditor')
                
                cursor.execute('SELECT MAX(last_updated) as last_update FROM accounts')
                last_update_row = cursor.fetchone()
                last_update = last_update_row['last_update'] if last_update_row else None
                
                debtors = [acc for acc in accounts if acc['type'] == 'debtor']
                creditors = [acc for acc in accounts if acc['type'] == 'creditor']
                
                response_data = {
                    'summary': {
                        'total_debtors': total_debtors,
                        'total_creditors': total_creditors,
                        'net_position': total_debtors - total_creditors,
                        'total_accounts': len(accounts),
                        'debtors_count': len(debtors),
                        'creditors_count': len(creditors),
                        'last_updated': last_update
                    },
                    'debtors': debtors,
                    'creditors': creditors,
                    'timestamp': datetime.now().isoformat()
                }
                
                return jsonify(response_data)
                
            except sqlite3.Error as e:
                logging.error(f"Database query error: {e}")
                return jsonify({'error': 'Database query failed'}), 500
            finally:
                conn.close()

    def get_db_connection(self):
        """Get database connection with error handling"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            logging.error(f"Database connection error: {e}")
            return None

    def start_api_server(self):
        """Start the Flask API server in a separate thread"""
        def run_server():
            port = int(os.environ.get('PORT', 5000))
            self.app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()
        print(f"API server started on port {os.environ.get('PORT', 5000)}")
        logging.info("API server started")

    def backup_database(self):
        backup_path = os.path.join(self.backup_dir, f"tally_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        try:
            if os.path.exists(self.db_path):
                shutil.copy2(self.db_path, backup_path)
                logging.info(f"Database backed up to {backup_path}")
        except Exception as e:
            logging.error(f"Database backup failed: {e}")

    def fetch_ledger_data(self, group_name):
        current_date = datetime.now().strftime('%d-%m-%Y')
        xml_request = f'''
        <ENVELOPE>
            <HEADER>
                <VERSION>1</VERSION>
                <TALLYREQUEST>Export</TALLYREQUEST>
                <TYPE>Collection</TYPE>
                <ID>LedgerUnderGroup</ID>
            </HEADER>
            <BODY>
                <DESC>
                    <STATICVARIABLES>
                        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                        <SVFROMDATE>01-04-2023</SVFROMDATE>
                        <SVTODATE>{current_date}</SVTODATE>
                    </STATICVARIABLES>
                    <TDL>
                        <TDLMESSAGE>
                            <COLLECTION NAME="LedgerUnderGroup" ISMODIFY="No">
                                <TYPE>Ledger</TYPE>
                                <FETCH>Name, ClosingBalance, Parent, BillDate</FETCH>
                                <FILTER>GroupFilter</FILTER>
                            </COLLECTION>
                            <SYSTEM TYPE="Formulae" NAME="GroupFilter">$Parent = "{group_name}"</SYSTEM>
                        </TDLMESSAGE>
                    </TDL>
                </DESC>
            </BODY>
        </ENVELOPE>
        '''
        
        try:
            logging.info(f"Attempting to fetch data for group: {group_name}")
            print(f"Attempting to fetch data for group: {group_name}")
            response = requests.post(
                self.tally_url,
                data=xml_request,
                headers={'Content-Type': 'text/xml'},
                timeout=30
            )
            
            response.raise_for_status()
            logging.info(f"HTTP Status: {response.status_code} for {group_name}")
            print(f"HTTP Status: {response.status_code} for {group_name}")
            
            if not response.text.strip():
                logging.warning(f"No data returned for {group_name}")
                print(f"No data returned for {group_name}")
                return []
                
            return self.parse_ledger_data(response.text, group_name)
                
        except requests.exceptions.ConnectionError as e:
            logging.error(f"Connection error for {group_name}: {e}")
            print(f"Connection error for {group_name}: {e}")
            return []
        except requests.exceptions.RequestException as e:
            logging.error(f"Request error for {group_name}: {e}")
            print(f"Request error for {group_name}: {e}")
            return []
        except Exception as e:
            logging.error(f"Unexpected error fetching {group_name}: {e}")
            print(f"Unexpected error fetching {group_name}: {e}")
            return []

    def parse_ledger_data(self, xml_data, group_name):
        accounts = []
        try:
            xml_clean = re.sub(r'\sxmlns(?::\w+)?="[^"]+"', '', xml_data, count=1)
            with open(f'tally_response_{group_name.lower().replace(" ", "_")}.xml', 'w', encoding='utf-8') as f:
                f.write(xml_clean)
            logging.info(f"Raw XML saved for {group_name}")
            print(f"Raw XML saved for {group_name} (first 500 chars): {xml_clean[:500]}")

            try:
                root = ET.fromstring(xml_clean)
            except ET.ParseError:
                xml_clean = re.sub(r'<([^>]+):([^>]+)>', r'<\1_\2>', xml_data)
                root = ET.fromstring(xml_clean)
            
            ledger_count = len(root.findall('.//LEDGER'))
            logging.info(f"Found {ledger_count} LEDGER elements for {group_name}")
            print(f"Found {ledger_count} LEDGER elements for {group_name}")
            if ledger_count == 0:
                logging.warning(f"No valid LEDGER elements found in response for {group_name}")
                print(f"No valid LEDGER elements found in response for {group_name}")
                return []

            for ledger in root.findall('.//LEDGER'):
                name_elem = ledger.find('.//NAME')
                if name_elem is None or not name_elem.text or not name_elem.text.strip():
                    name_elem = next((elem for elem in ledger.iter('NAME') if elem.text and elem.text.strip()), None)
                name = name_elem.text.strip() if name_elem is not None and name_elem.text else f"Unnamed_{hash(str(ledger))}"
                if name.startswith("Unnamed"):
                    logging.warning(f"Unnamed ledger detected: {name} - Full XML: {ET.tostring(ledger, encoding='unicode', method='xml')}")
                    print(f"Unnamed ledger detected: {name} - Full XML snippet: {ET.tostring(ledger, encoding='unicode', method='xml')[:200]}")
                
                logging.debug(f"Processing ledger: {name}")
                print(f"Processing ledger: {name}")
                
                balance_elem = next((ledger.find(f'.//{tag}') for tag in ['CLOSINGBALANCE', 'CLBALANCE', 'BALANCE', 'AMOUNT', 'BALANCEAMOUNT', 'DRCRBALANCE', 'OPENINGBALANCE', 'LEDGERBALANCE'] if ledger.find(f'.//{tag}') is not None), None)
                if balance_elem is None:
                    balance_elem = next((elem for elem in ledger.iter() if elem.tag in ['CLOSINGBALANCE', 'CLBALANCE', 'BALANCE', 'AMOUNT', 'BALANCEAMOUNT', 'DRCRBALANCE', 'OPENINGBALANCE', 'LEDGERBALANCE']), None)
                
                parent_elem = ledger.find('PARENT')
                due_date_elem = next((ledger.find(tag) for tag in ['BILLDATE', 'DUEDATE'] if ledger.find(tag) is not None), None)
                
                balance = 0.0
                if balance_elem is not None:
                    balance_text = balance_elem.text if balance_elem.text else '0'
                    is_debit = balance_elem.get('DR', 'No').lower() == 'yes'
                    is_credit = balance_elem.get('CR', 'No').lower() == 'yes'
                    balance = self.parse_currency(balance_text)
                    if is_debit:
                        balance = abs(balance)
                    elif is_credit:
                        balance = -abs(balance)
                    logging.debug(f"Found balance tag: {balance_elem.tag} with value: {balance_text}, Adjusted: {balance}")
                    print(f"Found balance tag: {balance_elem.tag} with value: {balance_text}, Adjusted: {balance}")
                else:
                    logging.warning(f"No balance tag found for {name}")
                    print(f"No balance tag found for {name}")
                
                parent = parent_elem.text.strip() if parent_elem is not None and parent_elem.text else group_name
                due_date = due_date_elem.text.strip() if due_date_elem is not None and due_date_elem.text else ""
                
                logging.debug(f"Parsed: {name}, Balance: {balance}, Parent: {parent}")
                print(f"Parsed: {name}, Balance: {balance}, Parent: {parent}")
                
                if abs(balance) > 0.01:
                    account_type = "debtor" if "debtor" in group_name.lower() else "creditor"
                    accounts.append({
                        'name': name,
                        'type': account_type,
                        'balance': abs(balance),
                        'due_date': due_date,
                        'parent': parent,
                        'last_updated': datetime.now().isoformat()
                    })
            
            logging.info(f"Extracted {len(accounts)} accounts for {group_name}")
            print(f"Extracted {len(accounts)} accounts for {group_name}")
            return accounts
        except ET.ParseError as e:
            logging.error(f"XML parsing error for {group_name}: {e} - Raw XML: {xml_data[:1000]}")
            print(f"XML parsing error for {group_name}: {e} - Raw XML snippet: {xml_data[:1000]}")
            return []
        except Exception as e:
            logging.error(f"Error processing {group_name} data: {e} - Raw XML: {xml_data[:1000]}")
            print(f"Error processing {group_name} data: {e} - Raw XML snippet: {xml_data[:1000]}")
            return []

    def parse_currency(self, currency_text):
        if not currency_text:
            return 0.0
        try:
            clean_text = re.sub(r'[₹,\s]', '', currency_text)
            clean_text = re.sub(r'\s*(Dr|Cr)\s*', '', clean_text, flags=re.IGNORECASE)
            match = re.search(r'-?\d+\.?\d*', clean_text)
            value = float(match.group()) if match else float(clean_text) if clean_text else 0.0
            logging.debug(f"Parsed currency '{currency_text}' to {value}")
            print(f"Parsed currency '{currency_text}' to {value}")
            return value
        except ValueError as e:
            logging.warning(f"Invalid currency format: {currency_text} - Error: {e}")
            print(f"Invalid currency format: {currency_text} - Error: {e}")
            return 0.0

    def save_to_database(self, accounts):
        self.backup_database()
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('PRAGMA table_info(accounts)')
            columns = [row[1] for row in cursor.fetchall()]
            if 'due_date' not in columns:
                cursor.execute('ALTER TABLE accounts ADD COLUMN due_date TEXT')
                logging.info("Added due_date column to accounts table")
            cursor.execute('DELETE FROM accounts')
            for account in accounts:
                cursor.execute('''
                    INSERT INTO accounts (name, type, closing_balance, due_date, parent_group, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    account['name'],
                    account['type'],
                    account['balance'],
                    account['due_date'],
                    account['parent'],
                    account['last_updated']
                ))
            conn.commit()
            logging.info(f"Saved {len(accounts)} accounts to database")
            print(f"Saved {len(accounts)} accounts to database")
        except sqlite3.Error as e:
            logging.error(f"Database save error: {e}")
            print(f"Database save error: {e}")
        finally:
            conn.close()

    def extract_and_save(self):
        logging.info(f"Starting data extraction at {datetime.now()}")
        print(f"Starting data extraction at {datetime.now()}")
        
        debtors = self.fetch_ledger_data("Sundry Debtors")
        creditors = self.fetch_ledger_data("Sundry Creditors")
        accounts = debtors + creditors
        
        if accounts:
            self.save_to_database(accounts)
            total_debtors = sum(a['balance'] for a in accounts if a['type'] == 'debtor')
            total_creditors = sum(a['balance'] for a in accounts if a['type'] == 'creditor')
            
            logging.info(f"Summary: {len(debtors)} Debtors (₹{total_debtors:,.2f}), {len(creditors)} Creditors (₹{total_creditors:,.2f})")
            print(f"Summary: {len(debtors)} Debtors (₹{total_debtors:,.2f}), {len(creditors)} Creditors (₹{total_creditors:,.2f})")
            
            print("\nSample accounts (up to 5):")
            for account in accounts[:5]:
                print(f"  {account['name']}: ₹{account['balance']:,.2f} ({account['type']})")
        else:
            logging.warning("No data extracted. Check Tally configuration and data availability.")
            print("No data extracted. Check the following:")
            print("- Ensure Tally Prime is running with a company loaded.")
            print("- Enable web server: F12 > Advanced Configuration > Enable Company on Web = Yes.")
            print("- Verify debtors/creditors exist under 'Sundry Debtors' or 'Sundry Creditors' groups with non-zero balances.")
            print("- Check console output, tally_extract.log, and XML files for details.")

        logging.info("Extraction complete")
        print("Extraction complete")

def main():
    print("Tally Dashboard Data Extractor with API - Enhanced Version")
    print("=========================================================")
    
    extractor = TallyDataExtractor()
    
    # Start API server first
    extractor.start_api_server()
    
    # Initial data extraction
    extractor.extract_and_save()
    
    # Schedule hourly updates
    schedule.every().hour.do(extractor.extract_and_save)
    
    print("System running:")
    print("- Data extractor: Updating every hour")
    print(f"- API server: Running on port {os.environ.get('PORT', 5000)}")
    print("- Press Ctrl+C to stop")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        logging.info("System stopped by user")
        print("\nSystem stopped")

if __name__ == "__main__":
    main()
