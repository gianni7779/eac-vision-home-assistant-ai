# Novità

## 0.1.2

- Analisi AI a 5 fotogrammi al secondo, con nuove analisi fino a tre volte al secondo durante il movimento: un passaggio breve può essere confermato in due fotogrammi come nella versione PC.
- Il movimento resta attivo per tre secondi dopo l'ultimo cambiamento; soglia regolata dalla sensibilità della telecamera e filtro per i cambi di luce estesi.
- «Analisi attiva» compare solo dopo il primo fotogramma ricevuto. Ogni camera mostra fotogrammi e analisi elaborati, eventuale video fermo e se è esclusa dal limite delle camere AI.

## 0.1.1

- Aggiunto il flusso HLS/TS come alternativa al formato fMP4 per i client iPhone con camere H.264.

## 0.1.0

- Prima edizione AI per mini PC amd64: stessa dashboard e registratore dell'edizione HA senza AI.
- Modello YOLOX Tiny della versione PC, inferenza CPU ONNX Runtime, rilevamento persone, veicoli e animali con filtro del movimento.
- Impostazioni AI per telecamera, eventi con istantanee e API per i client EAC Vision.
- Archivio con pulizia automatica, codice client personalizzabile, stato di CPU/memoria/disco e nomi delle camere nascondibili nel live.
- Anteprima del video durante il trascinamento della timeline delle registrazioni.
- Live HLS/fMP4 protetto dal codice client per il lettore iPhone.
