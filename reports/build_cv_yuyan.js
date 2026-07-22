const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, AlignmentType, LevelFormat,
  HeadingLevel, BorderStyle, ExternalHyperlink, TabStopType, TabStopPosition,
} = require("/root/.claude/skills/docx/node_modules/docx");

const ACCENT = "6B3FA0";   // purple
const DARK = "1A1A1A";
const GREY = "555555";

const rule = (color = ACCENT, size = 8) => ({
  bottom: { style: BorderStyle.SINGLE, size, color, space: 3 },
});

function sectionHeading(text) {
  return new Paragraph({
    spacing: { before: 260, after: 120 },
    border: rule(),
    children: [new TextRun({ text: text.toUpperCase(), bold: true, size: 24, color: ACCENT, font: "Calibri" })],
  });
}

function bullet(text, opts = {}) {
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { after: 60 },
    children: [new TextRun({ text, size: 21, color: opts.grey ? GREY : DARK, italics: !!opts.italics })],
  });
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Calibri", size: 21, color: DARK } } },
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
        alignment: AlignmentType.LEFT, style: { run: { color: ACCENT }, paragraph: { indent: { left: 360, hanging: 200 } } } }] },
    ],
  },
  sections: [{
    properties: {
      page: { size: { width: 12240, height: 15840 }, margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 } },
    },
    children: [
      // ---------- Header ----------
      new Paragraph({
        spacing: { after: 20 },
        children: [new TextRun({ text: "Yu Yan", bold: true, size: 52, color: DARK })],
      }),
      new Paragraph({
        spacing: { after: 80 },
        children: [new TextRun({ text: "Applied Scientist  |  Software Developer", size: 26, color: ACCENT })],
      }),
      new Paragraph({
        spacing: { after: 40 },
        children: [
          new TextRun({ text: "Sydney, Australia", size: 20, color: GREY }),
          new TextRun({ text: "      •      ", size: 20, color: GREY }),
          new ExternalHyperlink({
            link: "https://www.linkedin.com/in/yu-y-967989179/",
            children: [new TextRun({ text: "linkedin.com/in/yu-y-967989179", size: 20, color: ACCENT, underline: {} })],
          }),
        ],
      }),
      new Paragraph({ spacing: { after: 40 }, border: rule(ACCENT, 12), children: [new TextRun({ text: "" })] }),

      // ---------- Summary ----------
      sectionHeading("Professional Summary"),
      new Paragraph({
        spacing: { after: 80 },
        children: [new TextRun({
          text: "Applied Scientist and Software Developer at NinjaTech AI, an autonomous AI-agent company, "
            + "building and shipping agent-driven products. Holds a Master of Information Technology from the "
            + "University of Melbourne, combining applied machine-learning research with hands-on software "
            + "engineering. Based in Sydney, Australia.",
          size: 21,
        })],
      }),

      // ---------- Experience ----------
      sectionHeading("Experience"),
      new Paragraph({
        spacing: { after: 20 },
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        children: [
          new TextRun({ text: "Applied Scientist | Software Developer", bold: true, size: 22 }),
          new TextRun({ text: "\t2025 – Present", size: 20, color: GREY }),
        ],
      }),
      new Paragraph({
        spacing: { after: 80 },
        children: [new TextRun({ text: "NinjaTech AI  —  Sydney, Australia", italics: true, size: 21, color: GREY })],
      }),
      bullet("Applied scientist and software developer at NinjaTech AI, contributing to the company’s autonomous AI-agent platform."),
      bullet("[Add a key project or product you contributed to — e.g. a model, feature, or system you built.]", { grey: true, italics: true }),
      bullet("[Add a measurable achievement — e.g. improved a metric, shipped a capability, led an initiative.]", { grey: true, italics: true }),

      // ---------- Education ----------
      sectionHeading("Education"),
      new Paragraph({
        spacing: { after: 20 },
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        children: [
          new TextRun({ text: "Master of Information Technology", bold: true, size: 22 }),
        ],
      }),
      new Paragraph({
        spacing: { after: 80 },
        children: [new TextRun({ text: "The University of Melbourne  —  Melbourne, Australia", italics: true, size: 21, color: GREY })],
      }),

      // ---------- Skills ----------
      sectionHeading("Skills"),
      new Paragraph({
        spacing: { after: 60 },
        children: [
          new TextRun({ text: "Focus areas:  ", bold: true, size: 21 }),
          new TextRun({ text: "Applied machine learning · AI agents · Software development", size: 21 }),
        ],
      }),
      new Paragraph({
        spacing: { after: 60 },
        children: [new TextRun({
          text: "[Add specific skills to complete this section — e.g. Python, PyTorch/TensorFlow, LLMs, cloud (AWS/GCP), etc.]",
          italics: true, size: 20, color: GREY,
        })],
      }),

      // ---------- Footer note ----------
      new Paragraph({
        spacing: { before: 360, after: 0 },
        border: rule("CCCCCC", 4),
        children: [new TextRun({
          text: "Prepared by Ninja 🥷. Content in this CV is drawn only from Yu Yan’s public LinkedIn profile "
            + "(yu-y-967989179), NinjaTech AI, and the University of Melbourne. Italicised bracketed lines are "
            + "placeholders for details only Yu can confirm — no qualifications have been invented.",
          italics: true, size: 16, color: GREY,
        })],
      }),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync("/workspace/ninja/reports/Yu_Yan_CV.docx", buf);
  console.log("wrote reports/Yu_Yan_CV.docx", buf.length, "bytes");
});
