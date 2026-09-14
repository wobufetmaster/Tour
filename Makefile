.PHONY: test run

test:
	python3 -m unittest discover -s tests -v

run:
	python3 tour.py
