# Novità

## 0.1.9

- La scelta del video leggero per l'AI prova anche i profili ONVIF con nomi generici come `profile_2`, verificandone la risoluzione prima di usarli. Questo aiuta le telecamere che non chiamano il flusso secondario `subStream`.
- Live e registrazioni H24 continuano sul profilo principale; se un profilo AI non si apre o è troppo piccolo, resta attivo quello principale.

## 0.1.8

- Quando la camera offre un profilo ONVIF secondario compatibile, l'AI lo usa per ridurre il lavoro di decodifica; il live e le registrazioni H24 restano sul flusso principale.
- Il profilo leggero viene provato prima dell'uso. Se si interrompe o se la registrazione segnala un errore, l'analisi torna automaticamente al flusso principale.
- Meno copie di fotogrammi quando non c'è movimento e un solo thread OpenCV per limitare i picchi di CPU.

## 0.1.7

- Il lettore AI richiede solo la traccia video al relay e usa un singolo thread anche per l'uscita FFmpeg; eliminato un timeout I/O ridondante che poteva chiudere il flusso.
- Se il decoder si chiude prima del primo fotogramma, Impostazioni AI mostra il motivo restituito da FFmpeg, nascondendo eventuali URL e credenziali della telecamera.
- Il controllo automatico include un flusso RTSP sintetico attraverso go2rtc e FFmpeg, oltre al video campione con persone.

## 0.1.3

- La barra giornaliera delle registrazioni mostra dove l'AI ha rilevato persone (arancione), animali (verde) e veicoli (viola).
- L'API degli eventi accetta `day=AAAA-MM-GG` e legge gli eventi archiviati di quel giorno, anche se non sono più tra gli ultimi 500 in memoria. Le app possono mostrare gli stessi segni colorati.

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
