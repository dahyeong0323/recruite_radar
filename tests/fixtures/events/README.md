# Public Event fixtures

Captured 2026-10-06 from public official pages. HTML is reduced to the parser's
relevant nodes; content, selectors, event dates and structured data are preserved.

- friends.html: https://friendsofkorea.ch/event/ (upcoming modal)
- geneva.html: https://overseas.mofa.go.kr/ch-geneva-ko/brd/m_8846/list.do
- geneva-detail.html: same board, view.do?seq=1342688 (retrospective National Day event)
- bern-detail.html: https://che-berne.mofa.go.kr/ch-ko/brd/m_8038/view.do?seq=1345505
- sge.html: https://www.s-ge.com/export/en/events/all-events/upcoming-events
- sge-detail.html: https://www.s-ge.com/export/en/events/s-ge-event/winning-talent-race-attracting-and-retaining-talent-china-japan-and-southeast
- startupticker-detail.html: https://www.startupticker.ch/index.php/en/events/startup-nights-pitching-competition-2026

The KRX/Samsung test event is synthetic acceptance data, not an assertion that a
particular real roadshow is scheduled. S-GE's list parser remains disabled pending
verification of its Load More pagination. KOTRA's recorded legacy landing URL
returned HTTP 404; it remains disabled rather than pretending to collect.
