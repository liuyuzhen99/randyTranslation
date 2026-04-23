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

# summary

summarize what you've done at each phase at folder docs with file name phase\*-summary.md.
list down:

- what's the code and status before
- why it should change
- what's the current code, and why write code this way
- explain it with details so it's easier to understand
- the summerize should be also in detail and complete
- the file should be wrote in Chinese language

# tips

if you met network issue such as timeout during git push, run "zsh -lic 'proxy'" at the command line, that should fix the issue.

# frontend integration

frontend project git: https://github.com/liuyuzhen99/audit-flow.git, it's also located at /Users/randy/Documents/code/randyTranslation/vibeFrontTranslation
how to connect with Frontend:
┌─────────────────────────────────────────────────┐
│ 前端 (Next.js) │
└────────────────────┬────────────────────────────┘
│ HTTP / SSE / WebSocket
┌────────────────────▼────────────────────────────┐
│ BFF 层（Backend For Frontend） │
│ 按 UI Screen 组合数据，不暴露领域细节 │
│ 建议：直接放在你的 api/ 目录下，作为 router 层 │
└────────────────────┬────────────────────────────┘
│ 内部调用
┌────────────────────▼────────────────────────────┐
│ Domain Services（你的 application/） │
│ ArtistService / AuditService / PipelineService│
│ LibraryService / TranslationService │
└────────────────────┬────────────────────────────┘
│
┌────────────────────▼────────────────────────────┐
│ Infrastructure │
│ Spotify / YouTube RSS / Whisper / RAG / DB │
└─────────────────────────────────────────────────┘

- interface design with frontend: - page 1: artists - page 2: audit queue - page 3: pipeline - page 4: library
  you should check the frontend design and consider how to design the backend project
  You need to help me figure out the orchester and what service this backend can provide.
  you should also consider where to add the integration of manual review (specified at ideas) with frontend

# coding

each phase, if need extra resource such as install database, frontend test etc., complete the code part first and then stop and summarise what left to be done.
