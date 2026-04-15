# requirement

you're a high level senior backend dev and tech and architect, this project is willing to product value to customer and is the foundation of a future enterprise. You should consider high concurrency and data consistency. make sure right tasks won't fail, system won't get stuck, especially for a project which deal with many video and audio files. write the necessary code comment for better understanding, and every code must be tested successfully

# project background

this is a project about music video generation with subtitles automatically.
steps:

1. sync following artists from spotify regularly
2. get the youtube channel id of every artists regularly
3. fetch the latest official music video from youtube channel via rss (download audio first to speed up)
4. transcribe the music video subtitle via a AI transcriber
5. An AI reviewer will review the transcript and make slightly adjustment
6. An AI auditor will audit the music based on lyrics and music characteristics such as BPM, work density, energy. A RAG is there for auditor's reference to match the taste.
7. once audit passed, translator will come in and translate the lyrics to Chinese. it's also a AI translator and RAG is also there for translator to refer the nice examples of Chinese translations.
8. after translation, AI auditor will also review the translation to check the quality and make necessary adjustment.
9. video will be download it
10. use ffmpeg to wire bilingual subtitles to music video

# ideas

- manual review to be added after auditor audit the tastes
- during the automation process, if user found the music match user's taste, this music can be selected to add to taste RAG
- when checking the final result of wired video, if marked translation nice, this music can also be selected to add to translation RAG

# notes

do not consider the api folder as important as it is, the original code is just a test or hello world version of FastAPI. You need to help me figure out the orchester and what service this backend can provide.

# summary

summarize what you've done at each phase at folder docs with file name phase\*-summary.md.
list down:

- what's the code and status before
- why it should change
- what's the current code, and why write code this way
- explain it with details so it's easier to understand
- the summerize should be also in detail and complete
- the file should be wrote in Chinese language
