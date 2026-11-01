import { Showcase } from '@editonny/design-system/showcase';
import photo from '@editonny/design-system/assets/imagery/matheus-gomes.jpeg';
import br from '@editonny/design-system/assets/flags/br.svg';
import eg from '@editonny/design-system/assets/flags/eg.svg';
import es from '@editonny/design-system/assets/flags/es.svg';
import us from '@editonny/design-system/assets/flags/us.svg';
import city from '../../../../skills/editonny/visuals/assets/visuals/img/city.jpg';

const GUIDE = [
  { label: 'Home', href: '/en' },
  { label: 'Create a request', href: '/en/create' },
  { label: 'Catalog', href: '/en/catalog' },
  { label: 'Examples', href: '/en/examples' },
  { label: 'Installation', href: '/en/install' },
];

export default function DesignSystemPage() {
  return (
    <>
      <a className="sc-skip" href="#sc-main">Skip to content</a>
      <Showcase image={city.src} photo={photo.src} flags={{ br: br.src, eg: eg.src, es: es.src, us: us.src }} guide={GUIDE} />
    </>
  );
}
