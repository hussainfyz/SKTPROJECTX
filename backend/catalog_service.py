from .eci_client import ECIClient, ECIConfig

class CatalogService:
    """API-facing catalog service. UI-independent and reusable by other clients."""
    def __init__(self, config=None, client=None):
        self.config = config or ECIConfig()
        self.client = client or ECIClient(config=self.config)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self.client.close()

    def districts(self, state_code):
        return self.client.get_districts(state_code)

    def assemblies(self, district_code):
        return self.client.get_assemblies(district_code)

    def revisions(self, state_code, year):
        return self.client.get_publish_year_types(
            state_code=state_code, year=year, mis_key=getattr(self.config, "mis_key", "EROLLA32DVI09AJH")
        )

    def languages(self, payload):
        return self.client.get_ac_languages(payload)

    def parts(self, payload):
        return self.client.get_publish_part_list(payload)

    def captcha(self):
        return self.client.get_captcha()

    def save_captcha(self, captcha, path):
        return self.client.save_captcha_image(captcha, str(path))
