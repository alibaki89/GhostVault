# Build the report with XeLaTeX (TeX Live with fontspec, TikZ/pgfplots and the TeX Gyre fonts;
# Liberation Mono for code).
REPORT = ghostvault-report

$(REPORT).pdf: $(REPORT).tex $(wildcard sections/*.tex figures/*.tex images/*.png)
	latexmk -xelatex -interaction=nonstopmode -halt-on-error $(REPORT).tex

clean:
	latexmk -C $(REPORT).tex

.PHONY: clean
