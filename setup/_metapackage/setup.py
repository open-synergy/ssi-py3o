import setuptools

with open('VERSION.txt', 'r') as f:
    version = f.read().strip()

setuptools.setup(
    name="odoo14-addons-open-synergy-ssi-py3o",
    description="Meta package for open-synergy-ssi-py3o Odoo addons",
    version=version,
    install_requires=[
        'odoo14-addon-ssi_py3o',
        'odoo14-addon-ssi_py3o_print_mixin',
    ],
    classifiers=[
        'Programming Language :: Python',
        'Framework :: Odoo',
        'Framework :: Odoo :: 14.0',
    ]
)
