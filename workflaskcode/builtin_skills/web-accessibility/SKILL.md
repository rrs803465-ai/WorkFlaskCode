---
name: web-accessibility
description: Checklist for accessible HTML/CSS/JS when generating or editing web pages.
---
# Web accessibility

- Every `<img>` needs `alt` (empty for decorative images).
- Every form control needs a `<label for>` or `aria-label`.
- Use semantic landmarks: `header`, `nav`, `main`, `footer`.
- Set `lang` on `<html>` and a viewport meta tag.
- Keep colour contrast at or above 4.5:1 for body text, and never convey meaning with colour alone.
- All interactive elements must work with the keyboard and show a visible focus style.
