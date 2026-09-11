# Progress Guard — demo lokalne

React + TypeScript + Vite, zwykły CSS, bez backendu i zewnętrznych wywołań API. Wymagany Node.js 20.19+ lub 22.12+.

## Uruchomienie

W katalogu projektu:

```sh
npm install
npm run dev
```

Otwórz adres wypisany przez Vite (domyślnie http://127.0.0.1:5173).
Po instalacji zależności demo działa bez internetu. `npm run build` sprawdza TypeScript i tworzy `dist/`; `npm run preview` uruchamia podgląd zbudowanej aplikacji.

## Prezentacja

1. Domyślny tryb to OFF. Prompt jest wypełniony i można go edytować.
2. Start demo rozpoczyna przebieg. Next step pokazuje kolejny blok scenariusza.
3. Auto play przechodzi dalej co 1,8 s; Pause zatrzymuje odtwarzanie. Next step również przełącza na sterowanie ręczne.
4. Po zakończeniu pojawia się porównanie obu stałych scenariuszy.
5. Reset zeruje przebieg i metryki, zatrzymuje timer i zachowuje prompt oraz wybrany tryb. Przełącz ON, aby uruchomić drugi przebieg.
6. Guard ON zatrzymuje odtwarzanie na interwencji po dwóch takich samych błędach. Next step wznawia pracę agenta. Auto play można następnie ponownie włączyć.

Przełącznik trybu jest zablokowany podczas przebiegu; Reset odblokowuje go. Wszystkie kontrolki działają z klawiatury przez Tab i Enter/Spację.

## Zmiana scenariusza

`src/data/demoScenario.ts` zawiera prompt, fingerprint oraz oba przebiegi. Metryki w każdym kroku są **skumulowane**, a lista wyświetlanych tool calls jest reprezentatywnym skrótem aktywności. Edycja prompta zmienia wiadomość w czacie, nie zaprogramowany przebieg. Guard jest kartą refleksji, nie drugim agentem i nie podaje rozwiązania.

Porównanie wykorzystuje stałe demonstracyjne wartości: OFF 47 wywołań / 6 testów / 5 błędów / 18 400 tokenów; ON 29 / 3 / 2 / 12 800. ~30% jest szacunkiem demonstracyjnym, nie pomiarem produkcyjnym. Informacja o publicznych śladach wykonania pochodzi z briefu; repo nie zawiera źródłowego śladu.
