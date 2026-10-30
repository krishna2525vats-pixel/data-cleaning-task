# Commands (macOS / Linux)

cd data_cleaning_task
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python generate_messy_data.py
python clean_data.py
python -m unittest discover -s tests -v

# package
cd ..
zip -r data_cleaning_submission.zip data_cleaning_task -x "*/venv/*" "*/__pycache__/*"
