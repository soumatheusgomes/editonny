'use client';
import { Aurora, Button, Chip, Counter, CreatorChip, Highlight, KineticCaption, KineticText, LanguageSelect, MarkItem, Marquee, Reveal, SiteHeader, Timeline, Transport, Waveform } from '../components/index';
import { Force, OnMedia, Row, Section, Specimen, State, type Theme } from './kit';

const SEGMENTS = [{ start: 0, end: 6, beat: 'Hook', quote: 'Nobody told you' }, { start: 6, end: 19, beat: 'Problem', quote: 'Overheated water' }, { start: 19, end: 34, beat: 'Step by step', quote: '94 degrees, then the bloom' }, { start: 34, end: 45, beat: 'Call to action', quote: 'Save it to try' }];
const noop = () => {};

export type ShowcaseAssets = { image: string; photo: string; flags: Record<'br' | 'us' | 'es' | 'eg', string> };

const editor = (theme: Theme) => <>
  <Specimen theme={theme} name="Transport" description="Player controls: accent play button, mono timecode, frame and second steppers.">
    <Row label="States">
      <div className="sc-wide"><State label="Paused"><Transport time="00:12.48" duration="00:45.00" /></State></div>
      <div className="sc-wide"><State label="Playing"><Transport playing time="00:21.10" duration="00:45.00" /></State></div>
      <div className="sc-wide"><State label="Play focus"><Force value="focus" selector="button"><Transport time="00:00.00" duration="00:45.00" /></Force></State></div>
    </Row>
  </Specimen>
  <Specimen theme={theme} name="Timeline" description="Ruler, beat segments, waveform, hatched marks, and playhead; keyboard-operable when seekable.">
    <Row label="States">
      <div className="sc-full"><State label="Playing with a mark"><Timeline duration={45} playhead={22.5} playing segments={SEGMENTS} marks={[{ start: 22, end: 26, note: 'remove this part' }]} onSeek={noop} /></State></div>
      <div className="sc-full"><State label="Paused at start"><Timeline duration={45} segments={SEGMENTS} /></State></div>
      <div className="sc-full"><State label="Empty (no segments yet)"><Timeline duration={45} segments={[]} /></State></div>
    </Row>
  </Specimen>
  <Specimen theme={theme} name="MarkItem" description="Review note pinned to a time range; slides in when added.">
    <Row label="States">
      <div className="sc-wide"><State label="Default"><MarkItem index={1} range="00:12.4 – 00:15.0" note="tighten this pause" onRemove={noop} /></State></div>
      <div className="sc-wide"><State label="Current (under playhead)"><MarkItem index={2} range="00:22.0 – 00:26.0" note="remove this part, it repeats" current onRemove={noop} /></State></div>
      <div className="sc-wide"><State label="Read-only"><MarkItem index={3} range="00:40.0 – 00:42.5" note="add a call-to-action scene" /></State></div>
    </Row>
  </Specimen>
  <Specimen theme={theme} name="Waveform" description="Bars breathe while playing; cuts flatten removed silences; progress splits played and unplayed.">
    <Row label="States">
      <State label="Playing with cuts"><div className="sc-wide"><Waveform cuts={[[0.22, 0.3], [0.61, 0.66]]} /></div></State>
      <State label="Progress 40%"><div className="sc-wide"><Waveform progress={0.4} color="var(--surface-accent-2)" height={40} playing={false} /></div></State>
      <State label="Paused"><div className="sc-wide"><Waveform playing={false} bars={32} /></div></State>
    </Row>
  </Specimen>
  <Specimen theme={theme} name="KineticCaption" description="Word-by-word caption; the active word pops inside a highlight box. Always over footage.">
    <Row label="Variants">
      <State label="Clean, sun"><OnMedia><KineticCaption text="Nobody told you this about coffee" size={26} /></OnMedia></State>
      <State label="Impact, volt"><OnMedia><KineticCaption text="Stop scrolling right now" font="var(--font-caption-impact)" highlight="var(--volt-400)" size={30} /></OnMedia></State>
      <State label="Hand, paused, left"><OnMedia><KineticCaption text="Every word on time" font="var(--font-caption-hand)" uppercase={false} playing={false} align="left" size={30} /></OnMedia></State>
    </Row>
  </Specimen>
</>;

const motion = (theme: Theme) => <>
  <Specimen theme={theme} name="Marquee" description="Infinite Anton ticker; stack two in opposite directions for crossed tapes. Hover pauses.">
    <div className="sc-tapes">
      <Marquee tone="orange" size={40} rotate={-3} items={['Clean cuts', 'Captions', 'Motion', 'Soundtrack', '145 scenes']} />
      <Marquee tone="volt" size={30} reverse rotate={3} items={['9:16', '16:9', '1:1', '4:5', '100% local']} />
    </div>
    <Row label="Tones">{(['black', 'white', 'outline'] as const).map(tone => <State key={tone} label={tone}><div className="sc-full"><Marquee tone={tone} size={28} items={['Editor', 'Motion', 'Preview']} /></div></State>)}</Row>
    <Row label="Paused on hover"><State label="Hover"><div className="sc-full"><Force value="hover"><Marquee size={24} items={['Paused', 'while', 'hovered']} /></Force></div></State></Row>
  </Specimen>
  <Specimen theme={theme} name="KineticText" description="Rotating headline word inside a solid box; inherits the font size.">
    <Row label="Variants">
      <State label="Boxed"><div className="sc-headline">We give it <KineticText words={['rhythm.', 'cuts.', 'motion.', 'sound.']} /></div></State>
      <State label="Text only, volt"><div className="sc-headline"><KineticText boxed={false} box="var(--accent-2-text)" words={['fast', 'clean']} /></div></State>
    </Row>
  </Specimen>
  <Specimen theme={theme} name="Counter" description="Counts up with expo easing when it enters the viewport.">
    <Row label="Values">
      <State label="Scenes"><div className="sc-stat"><Counter value={145} /></div></State>
      <State label="Percent"><div className="sc-stat"><Counter value={100} suffix="%" /></div></State>
      <State label="Decimals"><div className="sc-stat"><Counter value={4.5} decimals={1} suffix="x" /></div></State>
    </Row>
  </Specimen>
  <Specimen theme={theme} name="Aurora" description="Animated hero light with film grain; fills its relative parent once per page.">
    <Row label="Palettes">{(['fire', 'volt', 'mono'] as const).map(palette => <State key={palette} label={palette}><div className="sc-aurora"><Aurora palette={palette} /><span>{palette}</span></div></State>)}</Row>
  </Specimen>
  <Specimen theme={theme} name="Reveal" description="Blur, slide, and fade in the first time content scrolls into view; stagger with delay.">
    <Row label="Staggered">{[0, 1, 2].map(i => <State key={i} label={`delay ${i * 110}ms`}><Reveal delay={i * 110}><div className="sc-chip">{`Item ${i + 1}`}</div></Reveal></State>)}</Row>
  </Specimen>
</>;

const site = (theme: Theme, assets: ShowcaseAssets) => {
  const languages = [
    { code: 'en-US', short: 'EN', native: 'English', sub: 'United States', flagUrl: assets.flags.us },
    { code: 'pt-BR', short: 'PT', native: 'Português', sub: 'Brasil', flagUrl: assets.flags.br },
    { code: 'es', short: 'ES', native: 'Español', sub: 'España · Latinoamérica', flagUrl: assets.flags.es },
  ];
  return <>
    <Specimen theme={theme} name="SiteHeader" description="Floating glass-pill header with wordmark, section links, language, and CTA.">
      <div className="sc-full">
        <SiteHeader fixed={false} active="#demo" navLabel={`Example main navigation, ${theme} theme`} links={[{ label: 'Demo', href: '#demo' }, { label: 'Features', href: '#features' }, { label: 'Library', href: '#library' }]}
          right={<><LanguageSelect options={languages} /><Button size="sm" icon="github">Get the skill</Button></>} />
      </div>
    </Specimen>
    <Specimen theme={theme} name="LanguageSelect" description="Round-flag pill opening a searchable list (↑ ↓ Enter Esc); current language tagged orange.">
      <Row label="States">
        <State label="Closed"><LanguageSelect options={languages} /></State>
        <State label="Trigger hover"><Force value="hover" selector="button"><LanguageSelect options={languages} value="pt-BR" /></Force></State>
        <State label="Open"><div className="sc-tall"><LanguageSelect options={languages} defaultOpen /></div></State>
      </Row>
    </Specimen>
    <Specimen theme={theme} name="CreatorChip" description="Glass pill with the creator photo in a spinning ring; once per page.">
      <Row label="Variants">
        <State label="With photo and link"><CreatorChip name="Matheus Gomes" role="Creator" status="Free · open source" photo={assets.photo} href="https://github.com/soumatheusgomes/editonny" /></State>
        <State label="Initial fallback, no ring"><CreatorChip name="Editonny" role="Guide" ring={false} /></State>
      </Row>
    </Specimen>
    <Specimen theme={theme} name="Highlight" description="Solid block behind the key phrase of a headline; one per headline.">
      <Row label="Tones">{(['orange', 'volt', 'white', 'sun'] as const).map(tone => <State key={tone} label={tone}><div className="sc-headline">Each request is <Highlight tone={tone} nowrap={tone === 'white'}>a layer.</Highlight></div></State>)}</Row>
    </Specimen>
    <Specimen theme={theme} name="Chip" description="Pill toggle for filters, chapters, and option pickers: outline idle, solid selected.">
      <Row label="States">
        <State label="Default"><Chip count={54}>All goals</Chip></State>
        <State label="Hover"><Force value="hover"><Chip>Education</Chip></Force></State>
        <State label="Active"><Force value="active"><Chip>Sales</Chip></Force></State>
        <State label="Focus"><Force value="focus"><Chip>Careers</Chip></Force></State>
        <State label="Selected"><Chip selected count={54}>All goals</Chip></State>
        <State label="Disabled"><Chip disabled>Soon</Chip></State>
      </Row>
      <Row label="Tones selected">
        <State label="orange"><Chip tone="orange" selected>01 Pauses</Chip></State>
        <State label="sun, caption font"><Chip tone="sun" selected font="var(--font-caption-impact)">IMPACT</Chip></State>
        <State label="small"><Chip size="sm" font="var(--font-caption-hand)">Handwritten</Chip></State>
      </Row>
    </Specimen>
  </>;
};

export const EditorSection = () => <Section id="editor" title="Editor" lead="Transport, Timeline, MarkItem, Waveform, and KineticCaption." render={editor} />;
export const MotionSection = () => <Section id="motion" title="Motion" lead="Marquee, KineticText, Counter, Aurora, and Reveal. All respect reduced motion." render={motion} />;
export const SiteSection = ({ assets }: { assets: ShowcaseAssets }) => <Section id="site" title="Site" lead="SiteHeader, LanguageSelect, CreatorChip, Highlight, and Chip." render={theme => site(theme, assets)} />;
